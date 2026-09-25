# SPDX-License-Identifier: Apache-2.0
"""Tests for the device-configuration registry, backup/restore, and safety gating.

All run against :class:`MockDevice` — no hardware required.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from meshterm.core.config_io import backup_config, plan_restore, read_backup
from meshterm.core.connection import MockDevice
from meshterm.core.device_config import (
    RADIO_PRESETS,
    DeviceConfigError,
    build_snapshot,
    current_preset,
    effective_maximum,
    format_value,
    get_spec,
    parse_value,
)
from meshterm.tools.config import _require_yes


async def _connected_mock() -> MockDevice:
    """Return a connected MockDevice."""
    device = MockDevice()
    await device.connect()
    return device


# -- parsing / formatting ------------------------------------------------------


def test_parse_value_types() -> None:
    """Each value type coerces from string correctly."""
    assert parse_value(get_spec("name"), "Node-7") == "Node-7"
    assert parse_value(get_spec("radio_sf"), "9") == 9
    assert parse_value(get_spec("radio_freq"), "868.0") == 868.0
    assert parse_value(get_spec("manual_add_contacts"), "yes") is True
    assert parse_value(get_spec("manual_add_contacts"), "off") is False


def test_parse_value_range_and_enum_errors() -> None:
    """Out-of-range and invalid enum values raise DeviceConfigError."""
    with pytest.raises(DeviceConfigError, match="<= 12"):
        parse_value(get_spec("radio_sf"), "99")
    with pytest.raises(DeviceConfigError, match=">= 5"):
        parse_value(get_spec("radio_cr"), "1")
    with pytest.raises(DeviceConfigError, match="one of"):
        parse_value(get_spec("telemetry_mode_base"), "7")
    with pytest.raises(DeviceConfigError, match="boolean"):
        parse_value(get_spec("manual_add_contacts"), "maybe")


def test_format_value() -> None:
    """Formatting renders booleans, enums, and unknowns readably."""
    assert format_value(get_spec("manual_add_contacts"), True) == "true"
    assert format_value(get_spec("telemetry_mode_base"), 1) == "1 (by contact)"
    assert format_value(get_spec("name"), None) == "?"


def test_path_hash_mode_is_strict_enum() -> None:
    """path_hash_mode takes the three modes the firmware accepts; it refuses a mode 3."""
    assert parse_value(get_spec("path_hash_mode"), "2") == 2
    assert format_value(get_spec("path_hash_mode"), 0) == "0 (1-byte hashes (default))"
    with pytest.raises(DeviceConfigError, match="one of"):
        parse_value(get_spec("path_hash_mode"), "3")


def test_device_pin_is_zero_or_six_digits() -> None:
    """The firmware refuses any other PIN, so the value is refused before it is sent."""
    spec = get_spec("device_pin")
    assert parse_value(spec, "0") == 0
    assert parse_value(spec, "123456") == 123456
    with pytest.raises(DeviceConfigError, match="six digits"):
        parse_value(spec, "1234")


def test_telemetry_modes_stop_at_the_firmware_s_three() -> None:
    """Deny, by contact, allow all — there is no fourth mode to offer."""
    with pytest.raises(DeviceConfigError, match="one of"):
        parse_value(get_spec("telemetry_mode_env"), "3")


def test_highlighted_hash_highlights_path_hash_prefix() -> None:
    """A key lane lights only its path-hash prefix.

    The full key is shown, with those bytes lit in the node's hash-derived palette
    hue — the same hue the node's name wears.
    """
    from meshterm.ui.theme import node_style
    from meshterm.ui.widgets import highlighted_hash

    pub = "aabbccddee" + "00" * 27
    # mode 2 => 3-byte hashes => first 6 hex chars are the addressable prefix.
    text = highlighted_hash(pub, prefix_bytes=3)
    assert text.plain == pub  # the full key is shown
    highlighted = [s for s in text.spans if s.style == node_style(pub)]
    assert len(highlighted) == 1
    assert text.plain[highlighted[0].start : highlighted[0].end] == "aabbcc"


def test_highlighted_hash_truncates_on_a_byte_boundary() -> None:
    """A key too long for its budget is truncated on a byte boundary.

    It keeps whole leading bytes (an even digit count) plus an ellipsis, padded to the
    exact width, so a half-byte digit never shows.
    """
    from meshterm.ui.widgets import highlighted_hash

    pub = "ab" * 32  # 64 hex digits
    # An even budget: width - 1 is odd, so the last (half-byte) digit is dropped and the
    # freed cell pads out — 14 digits (7 bytes) shown, not 15.
    text = highlighted_hash(pub, prefix_bytes=1, width=16)
    assert len(text.plain) == 16  # the lane still spans the full budget
    visible = text.plain.rstrip().rstrip("…")
    assert visible == "ab" * 7 and len(visible) % 2 == 0
    assert "…" in text.plain
    # An odd budget already lands on a boundary: width - 1 = 14 digits, no padding.
    assert highlighted_hash(pub, prefix_bytes=1, width=15).plain == "ab" * 7 + "…"


def _contacts_names(counts, sort: str, prefix_bytes: int = 3):
    """Render a contacts table for ``sort`` and return (table, name-column plain text)."""
    from meshterm.core.models import Contact
    from meshterm.ui.widgets import ContactsSort, contacts_table

    contacts = [
        Contact(name="Bob", public_key="9f1a2b" + "00" * 29, key_prefix="9f1a2b"),
        Contact(name="Alice", public_key="3d63c6" + "00" * 29, key_prefix="3d63c6"),
    ]
    group = contacts_table(
        "Homestead",
        "aabbcc" + "00" * 29,
        contacts,
        prefix_bytes,
        counts,
        ContactsSort.from_name(sort),
    )
    table = group.renderables[0]  # (table, blank line, legend)
    names = [c.plain if hasattr(c, "plain") else str(c) for c in table.columns[1].cells]
    return table, names


def test_contacts_table_lists_us_first_with_full_keys() -> None:
    """The contacts table pins our node first, sorts by name, and shows full highlighted keys."""
    counts = {"3d63c6000000": 14}  # Alice was overheard; Bob wasn't
    table, names = _contacts_names(counts, sort="name")

    # Column 1 is the name; us first, then contacts alphabetically (Alice before Bob).
    assert names[0].startswith("Homestead")
    assert names[1] == "Alice"
    assert names[2] == "Bob"

    # Packet counts (column 3): Alice shows her tally, Bob shows the em dash.
    pkts = [c.plain if hasattr(c, "plain") else str(c) for c in table.columns[3].cells]
    assert pkts[1] == "14"
    assert pkts[2] == "—"

    # Keys (column 4) are the full key with the path-hash prefix highlighted (no truncation
    # in the model; Rich ellipsizes only at render time when the terminal is too narrow).
    from meshterm.ui.theme import node_style

    keys = list(table.columns[4].cells)
    assert keys[1].plain == "3d63c6" + "00" * 29
    # The prefix is highlighted in the key's own hash-derived hue.
    assert any(s.style == node_style("3d63c6") for s in keys[1].spans)


def test_contacts_table_sorts_by_heard_and_packets() -> None:
    """Non-default sorts reorder contacts while keeping our node pinned first."""
    from datetime import timedelta

    from meshterm.core.models import Contact, utcnow

    # Bob was heard more recently than Alice, but Alice has more overheard packets.
    contacts = [
        Contact(
            name="Bob",
            public_key="9f1a2b" + "00" * 29,
            key_prefix="9f1a2b",
            last_seen=utcnow() - timedelta(minutes=5),
        ),
        Contact(
            name="Alice",
            public_key="3d63c6" + "00" * 29,
            key_prefix="3d63c6",
            last_seen=utcnow() - timedelta(days=2),
        ),
    ]
    counts = {"3d63c6000000": 14, "9f1a2b000000": 3}
    from meshterm.ui.widgets import ContactsSort, contacts_table

    def names(sort: ContactsSort):
        group = contacts_table("Us", "aabbcc" + "00" * 29, contacts, 3, counts, sort)
        cells = group.renderables[0].columns[1].cells
        return [c.plain if hasattr(c, "plain") else str(c) for c in cells]

    # Freshest first: Bob (5m) before Alice (2d) under the default (ascending-age) heard sort.
    assert names(ContactsSort.from_name("heard"))[1:] == ["Bob", "Alice"]
    # Most packets first: Alice (14) before Bob (3) under the default (descending) packets sort.
    assert names(ContactsSort.from_name("packets"))[1:] == ["Alice", "Bob"]
    # Descending flips it: oldest-heard first puts Alice (2d) above Bob (5m).
    assert names(ContactsSort(column="heard", ascending=False))[1:] == ["Alice", "Bob"]


def test_contacts_table_breaks_metric_ties_by_name_ascending() -> None:
    """Two contacts with the same metric keep an A→Z order in both sort directions (no flip)."""
    from datetime import timedelta

    from meshterm.core.models import Contact, utcnow
    from meshterm.ui.widgets import ContactsSort, ordered_contacts

    same = utcnow() - timedelta(minutes=5)
    contacts = [
        Contact(name="Charlie", public_key="aa" * 16, last_seen=same),
        Contact(name="Alice", public_key="bb" * 16, last_seen=same),
        Contact(name="Bob", public_key="cc" * 16, last_seen=utcnow() - timedelta(hours=3)),
    ]

    def order(ascending: bool) -> list[str]:
        sort = ContactsSort(column="heard", ascending=ascending)
        return [c.name for c in ordered_contacts(contacts, {}, sort)]

    # Freshest first: the two 5-min contacts lead, ordered Alice→Charlie, then Bob (3h).
    assert order(ascending=True) == ["Alice", "Charlie", "Bob"]
    # Oldest first: Bob leads, but the tie still breaks Alice→Charlie — not Charlie→Alice.
    assert order(ascending=False) == ["Bob", "Alice", "Charlie"]


async def test_contacts_tool_opens_sorted_by_heard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Contacts list opens most-recently-heard first — who's out there now, not a roll call."""
    from meshterm.core.models import Contact
    from meshterm.tools.contacts import ContactsTool
    from meshterm.ui import widgets

    captured: dict = {}
    real_order = widgets.ordered_contacts

    def fake_order(contacts, counts, sort):  # noqa: ANN001, ANN202
        captured["sort"] = sort
        return real_order(contacts, counts, sort)

    monkeypatch.setattr(widgets, "ordered_contacts", fake_order)

    class _Devstate:
        async def self_info(self) -> dict:
            return {"name": "Us", "public_key": "aa" * 32}

        async def contacts(self) -> list:
            return [Contact(name="Alice", public_key="d4" * 32)]

        async def path_hash_mode(self) -> int:
            return 0

    class _Ctx:
        devstate = _Devstate()
        repo = type("R", (), {"heard_nodes": staticmethod(lambda: [])})()
        ui = type("U", (), {"show": staticmethod(lambda *r: None)})()

    await ContactsTool().run(_Ctx(), {})  # type: ignore[arg-type]
    assert (captured["sort"].column, captured["sort"].ascending) == ("heard", True)


def _contacts_sort(name: str = "name"):
    """The interactive Contacts screen's sort: the shared contact list's four-column ring."""
    from meshterm.ui.contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING
    from meshterm.ui.widgets import ContactsSort

    return ContactsSort.from_name(name, SORT_COLUMNS, SORT_OPENS_ASCENDING)


def test_contacts_screen_ctrl_arrows_steer_the_sort() -> None:
    """Ctrl+arrows steer the Contacts sort.

    Ctrl+←/→ walk the shared four-column ring, wrapping, and Ctrl+↑/↓ force the
    direction — the Time Machine picker's keys, now the Contacts screen's too.
    """
    from meshterm.ui.contacts_screen import ContactsScreen

    screen = ContactsScreen("Us", "aabbcc" + "00" * 29, [], 3, {}, _contacts_sort())
    assert (screen._sort.column, screen._sort.ascending) == ("name", True)

    screen.handle("ctrl_right")  # name -> heard, opening in its natural (ascending) direction
    assert (screen._sort.column, screen._sort.ascending) == ("heard", True)
    screen.handle("ctrl_right")  # heard -> packets, which opens descending
    assert (screen._sort.column, screen._sort.ascending) == ("packets", False)
    screen.handle("ctrl_up")  # force ascending
    assert screen._sort.ascending is True
    screen.handle("ctrl_down")  # force descending
    assert screen._sort.ascending is False
    screen.handle("ctrl_right")  # packets -> hash, the ring's fourth column
    assert (screen._sort.column, screen._sort.ascending) == ("hash", True)
    screen.handle("ctrl_right")  # hash -> wraps back to name
    assert screen._sort.column == "name"
    screen.handle("ctrl_left")  # name -> wraps to hash
    assert screen._sort.column == "hash"


def test_contacts_screen_lists_contacts_in_the_shared_lanes() -> None:
    """Contacts are listed in the shared lanes, with our own node pinned first.

    They render in NAME/HEARD/PKTS/KEY; plain arrows only move the highlight, and
    Enter resolves the highlighted node.
    """
    import re

    from meshterm.core.models import Contact, utcnow
    from meshterm.ui.contacts_screen import _ARCHIVE, YOU, ContactsScreen
    from meshterm.ui.tui.screen import CANCEL

    contacts = [
        Contact(name="Alice", public_key="aa" * 32, last_seen=utcnow()),
        Contact(name="Bob", public_key="bb" * 32),
    ]
    counts = {"aa" * 6: 7}  # Alice was overheard; Bob never
    screen = ContactsScreen("Us", "cc" * 32, contacts, 1, counts, _contacts_sort())
    body = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in screen.render_body(72))

    # The shared column header (recorded sticky, so it pins once scrolled past)...
    assert "NAME" in body and "HEARD" in body and "PKTS" in body and "KEY" in body
    assert screen._sticky_headers
    # ...our own node leading the lanes, then the contacts with their tallies.
    choices = screen._choices()
    assert choices[0].value == YOU
    assert "(you)" in choices[0].label.plain
    assert "Alice" in body and "Bob" in body
    assert f"{7:>5}" in body  # Alice's overheard packets, right-aligned in its lane
    assert "never" in body  # Bob has no last_seen
    # Each contact row carries the Contact itself, so Enter hands the whole record on; the
    # tail closes the list with the archive action (there is no exit row — Esc leaves).
    assert choices[-1].value == _ARCHIVE
    assert all(isinstance(c.value, Contact) for c in choices[1:-1])

    # Plain arrows move the highlight without touching the sort.
    before = (screen._sort.column, screen._sort.ascending)
    screen.handle("down")
    assert (screen._sort.column, screen._sort.ascending) == before
    highlighted = screen._current_choice().value
    assert highlighted != YOU and isinstance(highlighted, Contact)

    # Enter resolves the highlighted contact (open_contacts opens its Node detail);
    # Esc still leaves with CANCEL.
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]
    screen.handle("enter")
    assert resolved == [highlighted]
    screen.handle("escape")
    assert resolved == [highlighted, CANCEL]


def test_archive_age_rungs_split_stale_from_never_heard() -> None:
    """An age rung takes only contacts once heard and since gone quiet; never-heard has its own.

    The ladder's second section is the plain predictable operation a ranking cannot express,
    and its one subtlety is that a contact with no advert time at all must not fall into it:
    a freshly added contact that hasn't had time to advert yet reads as infinitely old, and
    an age rung that swept it would be deleting nodes for being new.
    """
    from meshterm.core.contact_score import ContactSignals, ScoredContact
    from meshterm.core.models import Contact
    from meshterm.ui.sweep_screen import _DAY, _NEVER, victims_for

    def scored(name: str, days, score: float) -> ScoredContact:  # noqa: ANN001
        contact = Contact(name=name, public_key=f"{ord(name[0]):02x}" * 32)
        return ScoredContact(
            contact=contact,
            signals=ContactSignals(node="0" * 12, heard_age_days=days),
            score=score,
            percentile=int(score),
        )

    # Strongest first, as `rank_contacts` returns them.
    ranked = [
        scored("Fresh", 0.04, 90.0),
        scored("Quiet", 3.0, 70.0),
        scored("Old", 45.0, 50.0),
        scored("Ancient", 400.0, 30.0),
        scored("Unheard", None, 10.0),
    ]
    names = lambda picked: [v.contact.name for v in victims_for(ranked, ranked, picked)]  # noqa: E731

    # A week catches the two aged ones — never the fresh, and never the never-heard.
    assert set(names(("age", 7 * _DAY))) == {"Old", "Ancient"}
    # A year catches only the truly ancient.
    assert names(("age", 365 * _DAY)) == ["Ancient"]
    # The never-heard bucket is exactly the contact with no advert time.
    assert names(("age", _NEVER)) == ["Unheard"]
    # Weakest first on the age route too, so a reader skimming the preview's first screen
    # sees what they are least likely to want back.
    assert names(("age", 7 * _DAY)) == ["Ancient", "Old"]


async def test_archive_sweeps_under_a_progress_bar_and_reports_in_a_dialog() -> None:
    """The whole sweep, end to end: ladder → preview → amber confirm → bar → outcome dialog.

    Two things the busy overlay could not do (JP, 2026-08-10). It only ever said "working",
    and only in the gaps *between* screens — over the pushed contacts list it drew nothing
    at all, so a long sweep looked like a screen that had stopped answering while the arrow
    keys still moved a cursor nothing was being done with. And the count of what it removed
    went to a note the reader would only meet on their way off the screen. So: the app's
    progress dialog (which swallows every key for its lifetime) while it runs, and a
    dismissible dialog naming the count when it is done.
    """
    import asyncio
    import logging
    from datetime import timedelta
    from types import SimpleNamespace

    from meshterm.core.contact_score import ContactSignals
    from meshterm.core.models import Contact, utcnow
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.sweep_screen import archive_contacts
    from meshterm.ui.tui.progress import ProgressScreen
    from meshterm.ui.tui.session import TuiSession

    old = utcnow() - timedelta(days=400)
    victims = [
        Contact(
            name=f"Old{i}", public_key=f"{i:02x}" * 32, key_prefix=f"{i:02x}" * 6, last_seen=old
        )
        for i in range(3)
    ]
    removed_from_device: list[str] = []
    archived: list[str] = []

    class _Device:
        async def remove_contact(self, contact) -> None:  # noqa: ANN001
            await asyncio.sleep(0)  # a real companion command takes a turn of the loop
            removed_from_device.append(contact.name)

    class _Repo:
        @staticmethod
        def contact_signals(nodes):  # noqa: ANN001
            # Long silent, never messaged, heard a handful of times — the shape of exactly
            # what the sweep exists to clear out.
            return {
                node: ContactSignals(node=node, heard_age_days=400.0, packets=2, known_days=420.0)
                for node in nodes
            }

        @staticmethod
        def channel_post_counts():
            return {}

    session = TuiSession()
    ui = TuiUi(session)
    asked: list[dict] = []

    async def _dialog(prompt, buttons, **kw):  # noqa: ANN001
        asked.append({"prompt": prompt, "buttons": buttons, **kw})
        return True

    ui.dialog = _dialog  # type: ignore[method-assign]
    ctx = SimpleNamespace(
        ui=ui,
        log=logging.getLogger("test.archive"),
        repo=_Repo(),
        contact_store=SimpleNamespace(
            locked_keys=lambda *a: frozenset(),
            is_locked=lambda *a: False,
            archive=lambda dev, contact, when: archived.append(contact.name),
        ),
        watch_store=None,
        admin_store=None,
        devstate=SimpleNamespace(
            contacts=lambda: _contacts(victims),
            self_info=lambda: _self_info({}),
            invalidate_contacts=lambda: None,
        ),
        device=lambda: _device(_Device()),
    )

    task = asyncio.ensure_future(archive_contacts(ctx, "cc" * 32))

    # Walk past the five standing rungs into the second section and take "not heard in 1
    # year", which catches all three — the standing rungs keep a share, so none of them
    # ever takes a whole table.
    ladder = await _step_until_screen(session, lambda s: s.title.startswith("Archive contacts"))
    body = _screen_text(ladder)
    # A rung is counted in columns, and what it *keeps* — the number that has to fit the
    # device — leads what it archives.
    assert body.index("KEEPS") < body.index("ARCHIVES")
    for _ in range(9):
        ladder.handle("down")
    ladder.handle("enter")

    # The preview lists exactly who would go, and commits on its Apply row (the default).
    preview = await _step_until_screen(session, lambda s: "to archive" in getattr(s, "title", ""))
    body = _screen_text(preview)
    # The evidence, one lane per kind — never a score, and no longer a percentile either:
    # the row shows what was measured, and the list's order is the ranking.
    lanes = ["NAME", "HEARD", "PKTS", "MSGS", "HOPS"]
    assert [body.index(word) for word in lanes] == sorted(body.index(word) for word in lanes)
    assert all(v.name in body for v in victims)
    preview.handle("enter")

    # The confirm is amber and button-only: this is reversible, so it spends neither the
    # reserved red nor a typed word.
    bar = await _step_until_screen(session, lambda s: isinstance(s, ProgressScreen))
    assert asked and asked[0]["danger"] is True
    assert "destructive" not in asked[0]
    assert [label for label, _v in asked[0]["buttons"]] == ["Cancel", "Archive"]
    assert "restored" in asked[0]["prompt"]

    # The sweep runs under the progress dialog, which is the frontmost screen for its whole
    # lifetime — so the list underneath cannot be walked while it works.
    assert bar.title == "Archive contacts"
    assert bar.render_body(60)  # a real bar, with a real total to count down
    bar.handle("down")  # every key is swallowed; nothing underneath moves

    # …and the count lands in a dialog, not in a note read on the way out.
    done = await _step_until_screen(
        session, lambda s: not isinstance(s, ProgressScreen) and "archived" in _screen_text(s)
    )
    assert "archived 3 contacts" in _screen_text(done)
    done.handle("escape")
    assert await task == 3
    assert sorted(removed_from_device) == ["Old0", "Old1", "Old2"]
    # Off the device, but kept: every victim landed in the store as archived.
    assert sorted(archived) == ["Old0", "Old1", "Old2"]


def test_a_preview_row_leads_with_the_contacts_type_mark() -> None:
    """Each contact the sweep would take leads with its type mark, as the Contacts list's do.

    The mark is a lane of its own ahead of the name, in the type's colour rather than the
    name's. The header moves over with it — ``NAME`` over the name, every evidence word still
    over its values — and ``←→`` keeps the mark pinned beside the name it belongs to.
    """
    from meshterm.core.contact_score import ContactSignals, ScoredContact
    from meshterm.core.models import NODE_TYPE_REPEATER, Contact
    from meshterm.ui.sweep_screen import (
        _MARK_W,
        _NAME_W,
        _lane_widths,
        _preview_header,
        _preview_items,
        _victim_row,
    )
    from meshterm.ui.theme import name_style

    def scored(name: str, key: str, node_type: int | None) -> ScoredContact:
        return ScoredContact(
            contact=Contact(name=name, public_key=key, key_prefix=key[:12], node_type=node_type),
            signals=ContactSignals(node=key[:12], packets=3),
            score=1.0,
            percentile=1,
        )

    victims = [scored("Hilltop", "3d" * 32, NODE_TYPE_REPEATER), scored("Walker", "5e" * 32, None)]
    widths = _lane_widths(victims)
    hilltop, walker = (_victim_row(v, widths) for v in victims)
    assert hilltop.plain.startswith("▲ Hilltop")
    assert walker.plain.startswith("● Walker")  # never advertised a type: the plain node

    def styles_at(row, index: int) -> list[str]:  # noqa: ANN001
        return [str(span.style) for span in row.spans if span.start <= index < span.end]

    assert styles_at(hilltop, 0) == ["type.repeater"]
    assert styles_at(hilltop, hilltop.plain.index("Hilltop")) == [name_style("Hilltop", "3d" * 32)]

    # The header is drawn from the pointer column's edge and a row from just past it.
    header = _preview_header(widths, 72)
    assert header.index("NAME") == 2 + hilltop.plain.index("Hilltop")
    assert header.index("PKTS") + len("PKTS") - 1 == 2 + hilltop.plain.index("3")

    items = _preview_items(victims)
    rows = [c for c in items if isinstance(getattr(c, "value", None), ScoredContact)]
    assert len(rows) == 2 and all(c.hscroll_from == _MARK_W + _NAME_W for c in rows)


async def test_sparing_a_row_shrinks_the_sweep_without_leaving_the_preview() -> None:
    """Delete on a preview row lifts that contact out of the sweep, in place.

    The alternative was to back out and pick a shallower rung — which spares the one contact
    you recognised by also sparing forty you did not care about. The list refreshes through
    ``replace_items`` rather than being rebuilt, so the reader keeps their place instead of
    being dropped at the top of a list they were halfway down, and the row that survives is
    genuinely dropped from what the sweep goes on to archive.
    """
    import asyncio
    from types import SimpleNamespace

    from meshterm.core.contact_score import ContactSignals, ScoredContact
    from meshterm.core.models import Contact
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.sweep_screen import _preview
    from meshterm.ui.tui.session import TuiSession

    def scored(name: str, pct: int) -> ScoredContact:
        key = f"{ord(name[-1]):02x}" * 32
        return ScoredContact(
            contact=Contact(name=name, public_key=key, key_prefix=key[:12]),
            signals=ContactSignals(node=key[:12]),
            score=float(pct),
            percentile=pct,
        )

    victims = [scored("Weak", 3), scored("Middling", 9), scored("Keeper", 14)]
    session = TuiSession()
    ctx = SimpleNamespace(ui=TuiUi(session))
    task = asyncio.ensure_future(_preview(ctx, victims))

    screen = await _step_until_screen(session, lambda s: "to archive" in getattr(s, "title", ""))
    assert "3 contacts to archive" in screen.title
    # Every contact row can be spared; the Apply/Back pair cannot — there is nothing to
    # remove from a decision. And the percentile lane is pinned out of the ←→ scroll: it is
    # the reader's place in a list ordered by it.
    rows = [c for c in screen._choices() if isinstance(c.value, ScoredContact)]
    assert len(rows) == 3 and all(c.deletable and c.hscroll_from > 0 for c in rows)
    assert not any(c.deletable for c in screen._choices() if not isinstance(c.value, ScoredContact))

    # The cursor opens on Apply (the committing row is the default), so walk to the top of
    # the contacts and down to "Keeper", then spare it. The screen stays up — this is an
    # edit, not an exit.
    screen.handle("home")
    screen.handle("down")
    screen.handle("down")
    assert screen._current_choice().value.contact.name == "Keeper"
    screen.handle("delete")
    # Wait for the loop to take the delete — and prove the refresh landed on the *same*
    # screen object rather than a rebuilt one, which is what keeps the reader's place.
    same = await _step_until_screen(
        session, lambda s: "2 contacts to archive" in getattr(s, "title", "")
    )
    assert same is screen, "the preview refreshed in place rather than being rebuilt"
    body = _screen_text(screen)
    assert "Keeper" not in body
    assert "Weak" in body and "Middling" in body
    assert "Archive 2 contacts" in body  # the Apply row re-counts what is left

    # Committing now archives exactly the two that survived the edit: End lands on Back,
    # one step up is Apply.
    screen.handle("end")
    screen.handle("up")
    assert screen._current_choice().value == ("apply",)
    screen.handle("enter")
    assert await task is True
    assert [v.contact.name for v in victims] == ["Weak", "Middling"]


async def test_sparing_every_row_leaves_the_sweep_with_nothing_to_do() -> None:
    """A preview emptied by the reader backs out rather than confirming an empty archive."""
    import asyncio
    from types import SimpleNamespace

    from meshterm.core.contact_score import ContactSignals, ScoredContact
    from meshterm.core.models import Contact
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.sweep_screen import _preview
    from meshterm.ui.tui.session import TuiSession

    key = "aa" * 32
    only = ScoredContact(
        contact=Contact(name="Solo", public_key=key, key_prefix=key[:12]),
        signals=ContactSignals(node=key[:12]),
        score=1.0,
        percentile=1,
    )
    victims = [only]
    session = TuiSession()
    task = asyncio.ensure_future(_preview(SimpleNamespace(ui=TuiUi(session)), victims))
    screen = await _step_until_screen(session, lambda s: "to archive" in getattr(s, "title", ""))
    screen.handle("home")  # off the default Apply row and onto the only contact
    screen.handle("delete")
    assert await task is False
    assert victims == []


@pytest.mark.parametrize("platform_name", ["regular", "picocalc"])
def test_the_archive_preview_keeps_its_cursor_in_its_box_walking_both_ways(
    platform_name: str,
) -> None:
    """The preview's highlight stays inside its box however far the reader walks, and back.

    A long sweep scrolls, and it is a list with a prompt over a pinned column header. Both
    once let the cursor leave the window on the way back up: the prompt was counted twice in
    the line the frame keeps in view, so the real row rode above the box's top edge, and at
    the bottom the header's slide dropped the row the reader had just stepped onto. Walked
    through the real dialog compositor on each platform, at its own readable width.
    """
    from rich.text import Text

    from meshterm.core.contact_score import ContactSignals, ScoredContact
    from meshterm.core.models import Contact
    from meshterm.platforms import PICOCALC, REGULAR, set_platform
    from meshterm.ui.sweep_screen import _preview_screen
    from meshterm.ui.tui.frame import compose_dialog

    platform = {"regular": REGULAR, "picocalc": PICOCALC}[platform_name]
    set_platform(platform)

    def scored(index: int) -> ScoredContact:
        key = f"{index:02x}" * 32
        return ScoredContact(
            contact=Contact(name=f"Faraway {index}", public_key=key, key_prefix=key[:12]),
            signals=ContactSignals(node=key[:12], heard_age_days=40.0 + index, packets=index),
            score=float(index),
            percentile=index,
        )

    screen = _preview_screen([scored(i) for i in range(40)])
    cols, rows = platform.readable_cols, 24
    # The cursor opens on Apply, at the bottom. Walk to the first contact, down to Back,
    # and up again: every step is a paint the reader sees.
    walk = [None] + ["up"] * 45 + ["down"] * 45 + ["up"] * 45
    for step, key in enumerate(walk):
        if key is not None:
            screen.handle(key)
        box = Text.from_ansi(compose_dialog(screen, cols, rows)).plain
        assert "❯" in box, f"step {step} ({key}): the highlight left the box\n{box}"


async def test_deleting_one_contact_drops_it_from_the_device_and_the_store() -> None:
    """The single-contact delete: a red confirm, then both halves of the union forgotten.

    The list a screen shows is the device's contact table *unioned* with the contacts
    MeshTerm remembers for that device, so a removal that only reached the radio would be
    merged straight back on the next read and read as a screen that did nothing. It also
    drops the cached contacts so the list it returns to actually re-reads.
    """
    import logging
    from types import SimpleNamespace

    from meshterm.core.models import Contact
    from meshterm.ui.node_detail_screen import _remove_contact
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    hub = Contact(name="Hub", public_key="3d" * 32, key_prefix="3d" * 6)
    removed: list[str] = []
    forgotten: list[tuple[str, str]] = []
    invalidated: list[bool] = []

    class _Device:
        async def remove_contact(self, contact) -> None:  # noqa: ANN001
            removed.append(contact.name)

    ui = TuiUi(TuiSession())
    asked: list[dict] = []

    async def _dialog(prompt, buttons, **kw):  # noqa: ANN001
        asked.append({"prompt": prompt, "buttons": buttons, **kw})
        return True  # the committing button

    ui.dialog = _dialog  # type: ignore[method-assign]
    ctx = SimpleNamespace(
        ui=ui,
        log=logging.getLogger("test.remove"),
        contact_store=SimpleNamespace(
            locked_keys=lambda *a: frozenset(),
            is_locked=lambda *a: False,
            forget=lambda dev, key: forgotten.append((dev, key)),
        ),
        devstate=SimpleNamespace(invalidate_contacts=lambda: invalidated.append(True)),
        device=lambda: _device(_Device()),
    )

    assert await _remove_contact(ctx, hub, "cc" * 32, "Hub") is True
    # The confirm is the app's single-record delete: red, Cancel on the left, the
    # committing verb on the right and default. It says plainly that this one is final —
    # and points at the archive, which is the reversible way to free the same device slot.
    assert asked[0]["destructive"] is True
    assert [label for label, _v in asked[0]["buttons"]] == ["Cancel", "Delete"]
    assert asked[0]["default"] == 1
    prompt = asked[0]["prompt"]
    assert "Hub" in prompt and "for good" in prompt and "restored" in prompt
    assert "archive it instead" in prompt
    # Both halves of the union, then the cache the list re-reads through.
    assert removed == ["Hub"]
    assert forgotten == [("cc" * 32, "3d" * 32)]
    assert invalidated == [True]


async def test_cancelling_the_remove_confirm_touches_nothing() -> None:
    """Esc/Cancel on the confirm leaves the contact on the device and the caller's list."""
    import logging
    from types import SimpleNamespace

    from meshterm.core.models import Contact
    from meshterm.ui.node_detail_screen import _remove_contact
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    touched: list[str] = []

    class _Device:
        async def remove_contact(self, contact) -> None:  # noqa: ANN001
            touched.append(contact.name)

    ui = TuiUi(TuiSession())

    async def _declined(*a, **k):  # noqa: ANN001, ANN002, ANN003
        return None  # what Esc resolves a button dialog to

    ui.dialog = _declined  # type: ignore[method-assign]
    ctx = SimpleNamespace(
        ui=ui,
        log=logging.getLogger("test.remove"),
        contact_store=SimpleNamespace(
            locked_keys=lambda *a: frozenset(),
            is_locked=lambda *a: False,
            forget=lambda *a: touched.append("forgot"),
        ),
        devstate=SimpleNamespace(invalidate_contacts=lambda: touched.append("invalidated")),
        device=lambda: _device(_Device()),
    )

    assert (
        await _remove_contact(ctx, Contact(name="Hub", public_key="3d" * 32), "cc" * 32, "Hub")
        is False
    )
    assert touched == []


async def test_a_refused_removal_is_shown_and_the_contact_stays() -> None:
    """A device that refuses is reported in a dialog — the row must not vanish on a lie.

    Only the *device* can drop a contact from its table; if it refuses, forgetting our own
    half of the union would hide a contact the radio still holds. So nothing is forgotten,
    the failure is put in front of the reader, and the page stays open on the node.
    """
    import asyncio
    import logging
    from types import SimpleNamespace

    from meshterm.core.models import Contact
    from meshterm.ui.node_detail_screen import _remove_contact
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    forgotten: list = []

    class _Device:
        async def remove_contact(self, contact) -> None:  # noqa: ANN001
            await asyncio.sleep(0)
            raise RuntimeError("contact table is busy")

    session = TuiSession()
    ui = TuiUi(session)

    async def _accepted(*a, **k):  # noqa: ANN001, ANN002, ANN003
        return True

    ui.dialog = _accepted  # type: ignore[method-assign]
    ctx = SimpleNamespace(
        ui=ui,
        log=logging.getLogger("test.remove"),
        contact_store=SimpleNamespace(
            locked_keys=lambda *a: frozenset(),
            is_locked=lambda *a: False,
            forget=lambda *a: forgotten.append(a),
        ),
        devstate=SimpleNamespace(invalidate_contacts=lambda: forgotten.append("cache")),
        device=lambda: _device(_Device()),
    )

    task = asyncio.ensure_future(
        _remove_contact(ctx, Contact(name="Hub", public_key="3d" * 32), "cc" * 32, "Hub")
    )
    failed = await _step_until_screen(session, lambda s: "couldn't delete" in _screen_text(s))
    assert "contact table is busy" in _screen_text(failed)
    failed.handle("escape")
    assert await task is False
    assert forgotten == []  # the device still holds it, so neither do we forget it


async def test_a_contact_the_device_never_held_is_still_removed_here() -> None:
    """“Not on the device” is not a refusal — the removal finishes on our side, and says so.

    The list a screen removes from is the union of the device's table and the contacts
    MeshTerm remembers for it, so the entry may only ever have existed on our side (or the
    firmware dropped it since we read). Failing there left a row the reader could not get
    rid of. Instead the reader is told the device had no part in it, our half is forgotten,
    and the visit ends on the contact like any other removal.
    """
    import asyncio
    import logging
    from types import SimpleNamespace

    from meshterm.core.connection import ContactNotOnDeviceError
    from meshterm.core.models import Contact
    from meshterm.ui.node_detail_screen import _remove_contact
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    hub = Contact(name="Hub", public_key="3d" * 32, key_prefix="3d" * 6)
    forgotten: list[tuple[str, str]] = []
    invalidated: list[bool] = []

    class _Device:
        async def remove_contact(self, contact) -> None:  # noqa: ANN001
            await asyncio.sleep(0)
            raise ContactNotOnDeviceError(contact)

    session = TuiSession()
    ui = TuiUi(session)

    async def _accepted(*a, **k):  # noqa: ANN001, ANN002, ANN003
        return True

    ui.dialog = _accepted  # type: ignore[method-assign]
    ctx = SimpleNamespace(
        ui=ui,
        log=logging.getLogger("test.remove"),
        contact_store=SimpleNamespace(
            locked_keys=lambda *a: frozenset(),
            is_locked=lambda *a: False,
            forget=lambda dev, key: forgotten.append((dev, key)),
        ),
        devstate=SimpleNamespace(invalidate_contacts=lambda: invalidated.append(True)),
        device=lambda: _device(_Device()),
    )

    task = asyncio.ensure_future(_remove_contact(ctx, hub, "cc" * 32, "Hub"))
    told = await _step_until_screen(session, lambda s: "wasn't in this device" in _screen_text(s))
    text = _screen_text(told)
    assert "Hub" in text and "⚠" in text  # a warning, not the ✗ of a failed command
    told.handle("escape")
    assert await task is True
    assert forgotten == [("cc" * 32, "3d" * 32)]
    assert invalidated == [True]


async def _self_info(info):  # noqa: ANN001, ANN201
    """Await to the given self-info dict (the device read the ranking makes)."""
    return info


async def _true() -> bool:
    return True


async def _contacts(rows):  # noqa: ANN001
    return list(rows)


async def _device(device):  # noqa: ANN001
    return device


def _screen_text(screen) -> str:  # noqa: ANN001
    """Everything a pushed screen says, as one plain string."""
    import re

    body = screen.render_body(60)
    lines = body if isinstance(body, list) else list(body)
    drawn = [line() if callable(line) else line for line in lines]
    return re.sub(r"\[[0-9;]*m", "", " ".join(drawn) + " " + str(screen.title))


async def _step_until_screen(session, predicate, *, limit: int = 500):  # noqa: ANN001
    """Yield to the loop until the frontmost screen satisfies ``predicate``, then return it."""
    import asyncio

    for _ in range(limit):
        top = session.top
        if top is not None and predicate(top):
            return top
        await asyncio.sleep(0)
    raise AssertionError("the expected screen never reached the top of the stack")


async def test_the_list_rebuilds_when_the_detail_page_deletes_the_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contact removed on its own page is gone from the list you come back to.

    The detail page is where a single contact is deleted, so the list has to be told: it
    hands the page a contact and takes back whether that contact survived, re-reading the
    device and rebuilding when it didn't. Without the rebuild the reader returns to a row
    for a contact the radio no longer holds.
    """
    import asyncio
    import logging
    from types import SimpleNamespace

    from meshterm.core.models import Contact
    from meshterm.ui import node_detail_screen
    from meshterm.ui.contacts_screen import ContactsScreen, open_contacts
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    alice = Contact(name="Alice", public_key="aa" * 32)
    bob = Contact(name="Bob", public_key="bb" * 32)
    remaining = [alice, bob]

    detailed: list = []

    async def _fake_detail(ctx, contact):  # noqa: ANN001
        """Stand in for the page: report that it deleted whichever contact it was handed."""
        detailed.append(contact)
        remaining.remove(contact)
        return True

    monkeypatch.setattr(node_detail_screen, "open_node_detail", _fake_detail)

    session = TuiSession()
    ctx = SimpleNamespace(
        ui=TuiUi(session),
        log=logging.getLogger("test.contacts"),
        contact_store=None,
        devstate=SimpleNamespace(contacts=lambda: _contacts(remaining)),
    )
    task = asyncio.ensure_future(
        open_contacts(ctx, "Us", "cc" * 32, list(remaining), 1, {}, _contacts_sort())
    )

    listed = await _step_until_screen(session, lambda s: isinstance(s, ContactsScreen))
    assert "2 known" in listed.title
    listed.handle("down")  # off our own node, onto the first contact
    listed.handle("enter")

    # The page deleted it, so the list that comes back is a *new* one, re-read without it.
    rebuilt = await _step_until_screen(
        session, lambda s: isinstance(s, ContactsScreen) and "1 known" in s.title
    )
    assert rebuilt is not listed
    assert detailed == [alice]
    assert "Alice" not in _screen_text(rebuilt)
    assert "Bob" in _screen_text(rebuilt)

    rebuilt.handle("escape")
    await task


def test_contacts_screen_tail_offers_archive_only_when_populated() -> None:
    """A populated list closes with the archive action; an empty one has no tail at all."""
    from meshterm.core.models import Contact
    from meshterm.ui.contacts_screen import _ARCHIVE, ContactsScreen

    populated = ContactsScreen(
        "Us", "cc" * 32, [Contact(name="Alice", public_key="aa" * 32)], 1, {}, _contacts_sort()
    )
    assert populated._choices()[-1].value == _ARCHIVE

    empty = ContactsScreen("Us", "cc" * 32, [], 1, {}, _contacts_sort())
    values = [c.value for c in empty._choices()]
    assert _ARCHIVE not in values  # nothing to archive — no action row


def test_the_archived_row_appears_only_when_something_is_archived() -> None:
    """The way in to the archived list is drawn, with its tally, exactly when there is one.

    A screen should not offer a route into an empty list, and the tally rides the row so the
    count is readable without opening it — the sweep's output being visible from the sweep's
    own screen is what keeps archiving from being something you lose track of.
    """
    from meshterm.core.models import Contact
    from meshterm.ui.contacts_screen import _ARCHIVE, _ARCHIVED, ContactsScreen

    contacts = [Contact(name="Alice", public_key="aa" * 32)]

    none_yet = ContactsScreen("Us", "cc" * 32, contacts, 1, {}, _contacts_sort())
    assert _ARCHIVED not in [c.value for c in none_yet._choices()]

    some = ContactsScreen("Us", "cc" * 32, contacts, 1, {}, _contacts_sort(), archived=12)
    values = [c.value for c in some._choices()]
    # Under the archive action, because it is where the sweep's output went.
    assert values[-2:] == [_ARCHIVE, _ARCHIVED]
    row = next(c for c in some._choices() if c.value == _ARCHIVED)
    assert "View archived contacts" in row.label and "12" in row.label

    # It stands on its own for a device whose whole table has been swept: there is nothing
    # left to archive, but there is very much something to go and look at.
    swept = ContactsScreen("Us", "cc" * 32, [], 1, {}, _contacts_sort(), archived=3)
    values = [c.value for c in swept._choices()]
    assert _ARCHIVE not in values and values[-1] == _ARCHIVED


async def test_archiving_one_contact_takes_it_off_the_device_and_keeps_it() -> None:
    """The single-contact archive: an amber confirm, off the radio, stamped in the store.

    The reversible half of the pair the detail page offers. It wears ``danger`` rather than
    ``destructive`` and asks for no typed word, because everything it takes is recoverable —
    which is the whole reason the app has two caution tiers.
    """
    import logging
    from types import SimpleNamespace

    from meshterm.core.models import NODE_TYPE_REPEATER, Contact
    from meshterm.ui.node_detail_screen import _archive_contact
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.theme import name_style
    from meshterm.ui.tui.session import TuiSession

    hub = Contact(
        name="Hub", public_key="3d" * 32, key_prefix="3d" * 6, node_type=NODE_TYPE_REPEATER
    )
    removed: list[str] = []
    archived: list[tuple[str, str]] = []
    invalidated: list[bool] = []

    class _Device:
        async def remove_contact(self, contact) -> None:  # noqa: ANN001
            removed.append(contact.name)

    ui = TuiUi(TuiSession())
    asked: list[dict] = []

    async def _dialog(prompt, buttons, **kw):  # noqa: ANN001
        asked.append({"prompt": prompt, "buttons": buttons, **kw})
        return True

    ui.dialog = _dialog  # type: ignore[method-assign]
    ctx = SimpleNamespace(
        ui=ui,
        log=logging.getLogger("test.archive"),
        contact_store=SimpleNamespace(
            locked_keys=lambda *a: frozenset(),
            is_locked=lambda *a: False,
            archive=lambda dev, contact, when: archived.append((dev, contact.public_key)),
        ),
        devstate=SimpleNamespace(invalidate_contacts=lambda: invalidated.append(True)),
        device=lambda: _device(_Device()),
    )

    assert await _archive_contact(ctx, hub, "cc" * 32, "Hub") is True
    assert asked[0]["danger"] is True and "destructive" not in asked[0]
    assert [label for label, _v in asked[0]["buttons"]] == ["Cancel", "Archive"]
    assert asked[0]["default"] == 1
    prompt = asked[0]["prompt"]
    assert "restore it at any time" in prompt
    # The contact is named as the Contacts list names it — its type mark to the left of the
    # name, each in its own colour — while the sentence around them keeps the amber.
    assert prompt.plain.startswith("Archive ▲ Hub? ")

    def styles_at(index: int) -> list[str]:
        return [str(span.style) for span in prompt.spans if span.start <= index < span.end]

    assert styles_at(prompt.plain.index("▲")) == ["type.repeater"]
    assert styles_at(prompt.plain.index("Hub")) == [name_style("Hub", "3d" * 32)]
    assert styles_at(0) == styles_at(prompt.plain.index("restore")) == ["warn"]
    assert not prompt.style, "a base style would merge the amber's bold into the mark"
    # Off the radio, kept here, and the cached contact list dropped so the list re-reads.
    assert removed == ["Hub"]
    assert archived == [("cc" * 32, "3d" * 32)]
    assert invalidated == [True]


async def test_restoring_writes_the_contact_back_before_clearing_the_mark() -> None:
    """A refused write leaves the contact archived — never listed as live on a radio without it."""
    import logging
    from types import SimpleNamespace

    from meshterm.core.connection import DeviceCommandError
    from meshterm.core.models import Contact
    from meshterm.ui.node_detail_screen import _restore_archived
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    hub = Contact(name="Hub", public_key="3d" * 32, key_prefix="3d" * 6)
    restored: list[str] = []

    class _Refuses:
        async def add_contact(self, contact) -> None:  # noqa: ANN001
            raise DeviceCommandError("contact table full")

    session = TuiSession()
    ctx = SimpleNamespace(
        ui=TuiUi(session),
        log=logging.getLogger("test.restore"),
        contact_store=SimpleNamespace(
            locked_keys=lambda *a: frozenset(),
            is_locked=lambda *a: False,
            restore=lambda dev, key: restored.append(key),
        ),
        devstate=SimpleNamespace(invalidate_contacts=lambda: None),
        device=lambda: _device(_Refuses()),
    )

    import asyncio

    task = asyncio.ensure_future(_restore_archived(ctx, hub, "cc" * 32, "Hub"))
    shown = await _step_until_screen(session, lambda s: "couldn't restore" in _screen_text(s))
    shown.handle("escape")
    assert await task is False
    assert restored == [], "the archive mark survives a write the device refused"


def test_non_strict_enum_accepts_unlisted_value() -> None:
    """multi_acks/adv_loc_policy list common values but still accept other ints."""
    spec = get_spec("multi_acks")
    assert spec.value_type == "enum" and not spec.strict_choices
    assert parse_value(spec, "5") == 5  # not in choices, but >= minimum: accepted
    with pytest.raises(DeviceConfigError, match=">= 0"):
        parse_value(spec, "-1")  # still range-checked


def test_radio_presets_map_to_radio_settings() -> None:
    """Every preset exposes the four radio fields by their registry keys."""
    assert RADIO_PRESETS, "expected at least one standard radio preset"
    for preset in RADIO_PRESETS:
        settings = preset.as_settings()
        assert {"radio_freq", "radio_bw", "radio_sf", "radio_cr"} <= set(settings)
        # Each value parses cleanly through its setting spec.
        for key, value in settings.items():
            assert parse_value(get_spec(key), value) == value


def test_radio_presets_match_meshcore_list() -> None:
    """Spot-check the presets against MeshCore's own suggested settings.

    The values are MeshCore's, not ours: a mesh only hears a node whose four radio
    parameters match, so a drifted table is a radio that talks to nobody. These are the
    ones a MeshTerm user is most likely to pick, including the two regions MeshCore now
    marks deprecated (still listed, because nodes that never re-tuned are on them).
    """
    by_name = {p.name: p for p in RADIO_PRESETS}
    expected = {
        "EU/UK (Narrow)": (869.618, 62.5, 8, 8),
        "EU/UK (Deprecated)": (869.525, 250.0, 11, 5),
        "USA/Canada (Recommended)": (910.525, 62.5, 7, 5),
        "Australia": (915.800, 250.0, 10, 5),
        "New Zealand (Narrow)": (917.375, 62.5, 7, 5),
    }
    for name, values in expected.items():
        preset = by_name[name]
        assert (preset.freq, preset.bw, preset.sf, preset.cr) == values
    # A region that names a path-hash size stages it as the firmware's mode (size - 1).
    assert by_name["New Zealand (Narrow)"].as_settings()["path_hash_mode"] == 1
    assert "path_hash_mode" not in by_name["EU/UK (Narrow)"].as_settings()


def test_radio_preset_summary_reads_as_meshcore_prints_it() -> None:
    """The row's parameter lane is MeshCore's own order, at MeshCore's precision."""
    by_name = {p.name: p for p in RADIO_PRESETS}
    assert by_name["USA/Canada (Recommended)"].summary == "910.525 SF7 BW62.5 CR5"
    assert by_name["Australia"].summary == "915.800 SF10 BW250 CR5"


async def test_current_preset_identifies_the_mock_radio() -> None:
    """The mock boots on the firmware defaults, which are EU/UK (Narrow) but for CR."""
    snapshot = await build_snapshot(await _connected_mock())
    assert current_preset(snapshot) is None  # CR5, where the EU preset asks CR8
    assert current_preset({**snapshot, "radio_cr": 8}).name == "EU/UK (Narrow)"


def test_current_preset_none_for_unmatched_radio() -> None:
    """A hand-tuned radio matches no preset, and a missing field never crashes it."""
    assert (
        current_preset({"radio_freq": 868.0, "radio_bw": 250.0, "radio_sf": 11, "radio_cr": 5})
        is None
    )
    assert current_preset({}) is None


def test_get_spec_unknown_raises() -> None:
    """An unknown key produces a helpful error listing valid keys."""
    with pytest.raises(DeviceConfigError, match="unknown setting"):
        get_spec("not_a_setting")


# -- apply against the mock device ---------------------------------------------


async def test_apply_simple_setting() -> None:
    """Setting the node name round-trips through the device."""
    device = await _connected_mock()
    spec = get_spec("name")
    await spec.apply(device, parse_value(spec, "Yagi-Hub"), await build_snapshot(device))
    assert (await device.get_self_info())["name"] == "Yagi-Hub"


async def test_apply_coupled_radio_field_preserves_others() -> None:
    """Changing one radio field rebuilds set_radio from the current snapshot."""
    device = await _connected_mock()
    spec = get_spec("radio_sf")
    await spec.apply(device, 9, await build_snapshot(device))
    info = await device.get_self_info()
    assert info["radio_sf"] == 9
    assert info["radio_freq"] == 869.618  # unchanged
    assert info["radio_bw"] == 62.5
    assert info["radio_cr"] == 5


async def test_apply_telemetry_mode_preserves_siblings() -> None:
    """Editing one telemetry mode leaves the other two intact."""
    device = await _connected_mock()
    spec = get_spec("telemetry_mode_loc")
    await spec.apply(device, 2, await build_snapshot(device))
    info = await device.get_self_info()
    assert info["telemetry_mode_loc"] == 2
    assert info["telemetry_mode_base"] == 0
    assert info["telemetry_mode_env"] == 0


async def test_custom_var_round_trip() -> None:
    """Custom variables persist on the device."""
    device = await _connected_mock()
    await device.set_custom_var("exp_flag", "1")
    assert (await device.get_custom_vars())["exp_flag"] == "1"


# -- backup / restore ----------------------------------------------------------


async def test_backup_and_read_round_trip(tmp_path: Path) -> None:
    """A backup writes all settings, custom vars, and channels and reads back."""
    device = await _connected_mock()
    await device.set_name("Foo")
    await device.set_channel(0, "Public", b"\x01" * 16)
    snapshot = await build_snapshot(device)
    path = backup_config(tmp_path / "cfg.toml", snapshot, {"exp": "1"}, await _channels(device))

    backup = read_backup(path)
    assert backup.settings["name"] == "Foo"
    assert backup.settings["radio_sf"] == 8
    assert backup.custom["exp"] == "1"
    assert backup.channels[0]["name"] == "Public"
    assert backup.channels[0]["secret"] == ("01" * 16)


async def test_plan_restore_emits_only_differences(tmp_path: Path) -> None:
    """The restore planner returns ops only for values that differ from the device."""
    source = await _connected_mock()
    await source.set_name("Foo")
    path = backup_config(tmp_path / "cfg.toml", await build_snapshot(source), {"exp": "1"}, [])
    backup = read_backup(path)

    target = await _connected_mock()  # fresh device: name still "MockCompanion"
    ops = plan_restore(backup, await build_snapshot(target), await target.get_custom_vars())

    assert ("set", "name", "Foo") in ops
    assert ("set_custom", "exp", "1") in ops
    # Unchanged values (e.g. radio_sf 8 == 8) are not re-applied.
    assert "radio_sf" not in [op[1] for op in ops if op[0] == "set"]


# -- the device-reported TX power ceiling ---------------------------------------


def test_tx_power_capped_by_device_reported_maximum() -> None:
    """With a snapshot, the board's max_tx_power tightens the static bound."""
    spec = get_spec("tx_power")
    snapshot = {"max_tx_power": 22}
    assert parse_value(spec, "22", snapshot) == 22
    with pytest.raises(DeviceConfigError, match="<= 22"):
        parse_value(spec, "23", snapshot)
    # Without a snapshot only the static ceiling applies.
    assert parse_value(spec, "27") == 27
    assert effective_maximum(spec, snapshot) == 22
    assert effective_maximum(spec, None) == 30


def test_effective_maximum_never_loosens_the_static_bound() -> None:
    """A device reporting a max above the static ceiling doesn't raise it."""
    spec = get_spec("tx_power")
    assert effective_maximum(spec, {"max_tx_power": 99}) == 30
    assert effective_maximum(spec, {"max_tx_power": "bogus"}) == 30  # unparseable -> static


# -- flood scope / auto-add / TX delays ------------------------------------------


def test_flood_scope_length_limited_and_formats_empty() -> None:
    """The scope name respects its 31-byte protocol slot; empty renders readably."""
    spec = get_spec("flood_scope")
    assert parse_value(spec, "alpha") == "alpha"
    with pytest.raises(DeviceConfigError, match="at most 30"):
        parse_value(spec, "x" * 31)
    assert format_value(spec, "") == "(not set)"
    assert format_value(spec, "alpha") == "alpha"
    with pytest.raises(DeviceConfigError, match="no spaces or commas"):
        parse_value(spec, "north shore")
    with pytest.raises(DeviceConfigError, match="30 bytes"):
        parse_value(spec, "é" * 16)  # 16 characters, 32 bytes


async def test_flood_scope_round_trips_with_hashtag_normalization() -> None:
    """A scope name is stored bare, as the firmware and apps store it; empty clears it."""
    device = await _connected_mock()
    spec = get_spec("flood_scope")
    await spec.apply(device, "#alpha", await build_snapshot(device))
    assert (await build_snapshot(device))["flood_scope"] == "alpha"
    await spec.apply(device, "", await build_snapshot(device))
    assert (await build_snapshot(device))["flood_scope"] == ""


async def test_autoadd_config_round_trips() -> None:
    """The auto-add bitmask reaches the device and reads back into the snapshot."""
    device = await _connected_mock()
    spec = get_spec("autoadd_config")
    await spec.apply(device, parse_value(spec, "5"), await build_snapshot(device))
    assert (await build_snapshot(device))["autoadd_config"] == 5


def test_tuning_fields_are_floats_with_firmware_ranges() -> None:
    """RX delay and airtime factor take real (unscaled) float values, firmware-bounded.

    The firmware stores both as floats (rx_delay_base 0–20 s, airtime_factor 0–9) and
    only the *wire* carries them ×1000 — the settings must speak the real units.
    """
    assert parse_value(get_spec("rx_delay"), "0.5") == 0.5
    assert parse_value(get_spec("airtime_factor"), "2.5") == 2.5
    with pytest.raises(DeviceConfigError, match="<= 20"):
        parse_value(get_spec("rx_delay"), "500")  # a raw wire value must be rejected
    with pytest.raises(DeviceConfigError, match="<= 9"):
        parse_value(get_spec("airtime_factor"), "1000")


def test_rx_delay_is_rounded_to_one_place() -> None:
    """RX delay shows and applies at one decimal, matching the repeater admin's delays."""
    spec = get_spec("rx_delay")
    assert parse_value(spec, "0.3333") == 0.3
    assert format_value(spec, 0.123) == "0.1"
    assert format_value(spec, 0.0) == "0.0"


def test_tx_delay_factors_are_not_companion_settings() -> None:
    """The repeater-only TX delay factors must not appear as (no-op) device settings.

    Companion firmware's CMD_SET_TUNING_PARAMS reads exactly rx_delay + airtime_factor
    and ignores trailing bytes, so offering these knobs here would silently do nothing.
    """
    with pytest.raises(DeviceConfigError, match="unknown setting"):
        get_spec("tx_delay_factor")
    with pytest.raises(DeviceConfigError, match="unknown setting"):
        get_spec("direct_tx_delay_factor")


async def test_apply_tuning_field_preserves_the_other() -> None:
    """Editing one tuning field resends the pair without clobbering its sibling."""
    device = await _connected_mock()
    await device.set_tuning(0.5, 2.0)
    spec = get_spec("airtime_factor")
    await spec.apply(device, 3.5, await build_snapshot(device))
    assert await device.get_tuning() == {"rx_delay": 0.5, "airtime_factor": 3.5}


# -- clock sync -------------------------------------------------------------------


async def test_mock_clock_drifts_until_set_time_corrects_it() -> None:
    """The simulator's clock reads behind the host until set_time re-anchors it."""
    import time as _time

    device = await _connected_mock()
    before = await device.get_time()
    assert before is not None and before < int(_time.time())  # seeded drift
    now = int(_time.time())
    await device.set_time(now)
    after = await device.get_time()
    assert after is not None and abs(after - now) <= 1


# -- the Device info status panel -------------------------------------------------


async def test_status_panel_reports_role_battery_clock_and_stats() -> None:
    """The info tool's status panel surfaces the read-only device state in one place."""
    import io

    from rich.console import Console

    from meshterm.tools.info import _status_panel
    from meshterm.ui.theme import MESH_THEME

    device = await _connected_mock()
    panel = await _status_panel(device, await build_snapshot(device))
    console = Console(theme=MESH_THEME, file=io.StringIO(), width=100)
    console.print(panel)
    out = console.file.getvalue()

    assert "companion" in out  # adv_type 1 rendered as the node role
    assert "MeshCore Simulator" in out
    assert "4.10 V" in out
    assert "1d 2h 3m" in out  # 93784 s of simulated uptime
    assert "s behind" in out  # the mock's seeded clock drift
    assert "-110 dBm noise floor" in out
    assert "210 sent" in out and "1234 received" in out


def test_uptime_renders_compact_units() -> None:
    """Uptime shows seconds only under a minute, then d/h/m parts as needed."""
    from meshterm.tools.info import _uptime

    assert _uptime(45) == "45 s"
    assert _uptime(3660) == "1h 1m"
    assert _uptime(93784) == "1d 2h 3m"
    assert _uptime(86400) == "1d 0m"


# -- safety gating -------------------------------------------------------------


def test_require_yes_blocks_without_confirmation() -> None:
    """Destructive CLI commands abort unless --yes is passed.

    As a *usage* error: a missing confirmation is a bad invocation, so it exits under the
    parser's own status and prints in the parser's own words on stderr, rather than a red
    line of its own on stdout (see :mod:`meshterm.core.exitcodes`).
    """
    with pytest.raises(typer.BadParameter, match="--yes"):
        _require_yes(False, "factory reset erases all data")
    _require_yes(True, "factory reset erases all data")  # does not raise


async def _channels(device: MockDevice) -> list[dict]:
    """Collect the mock's configured channels for backup."""
    channels = []
    for idx in range(8):
        ch = await device.get_channel(idx)
        if ch:
            channels.append(ch)
    return channels


async def test_the_preview_opens_node_pages_with_no_management_verbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enter on a archive candidate opens its page to *look* at, not to act on.

    The rule (JP, 2026-09-01): management verbs belong to the list a contact actually lives
    in, never to a page opened out of a list that is about to act on it wholesale. A preview
    whose whole job is choosing what to archive should not also hand out a singular archive —
    or a delete — from inside its own candidate list, which is two answers to one question.
    """
    import asyncio
    from types import SimpleNamespace

    import meshterm.ui.node_detail_screen as nd
    from meshterm.core.contact_score import ContactSignals, ScoredContact
    from meshterm.core.models import Contact
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.sweep_screen import _preview
    from meshterm.ui.tui.session import TuiSession

    opened: list[dict] = []

    async def _fake_detail(ctx, contact, *, manage=True):  # noqa: ANN001
        opened.append({"name": contact.name, "manage": manage})
        return False

    monkeypatch.setattr(nd, "open_node_detail", _fake_detail)

    key = "aa" * 32
    victims = [
        ScoredContact(
            contact=Contact(name="Solo", public_key=key, key_prefix=key[:12]),
            signals=ContactSignals(node=key[:12]),
            score=1.0,
            percentile=1,
        )
    ]
    session = TuiSession()
    task = asyncio.ensure_future(_preview(SimpleNamespace(ui=TuiUi(session)), victims))
    screen = await _step_until_screen(session, lambda s: "to archive" in getattr(s, "title", ""))
    screen.handle("home")  # off the default Apply row and onto the contact
    screen.handle("enter")
    await _step_until_screen(session, lambda s: bool(opened))
    assert opened == [{"name": "Solo", "manage": False}]

    # The preview is still up underneath — looking at a candidate is not leaving the sweep.
    assert session.top is screen
    screen.handle("escape")
    assert await task is False


def test_a_locked_contact_leads_its_row_with_a_padlock_left_of_its_type_glyph() -> None:
    """Locked rows open on the padlock; the rest keep the column blank, so glyphs line up."""
    from meshterm.core.models import Contact
    from meshterm.ui.contacts_screen import ContactsScreen

    contacts = [
        Contact(name="Al", public_key="aa" * 32),
        Contact(name="Bartholomew", public_key="bb" * 32),
    ]
    screen = ContactsScreen(
        "Us", "dd" * 32, contacts, 1, {}, _contacts_sort(), locked=frozenset({"aa" * 32})
    )
    screen.note_viewport(30)
    screen.render_body(72)
    lines = {
        name: choice.title.plain
        for choice in screen._choices()
        for name in ("Al", "Bartholomew")
        if getattr(choice.value, "name", None) == name
    }
    assert lines["Al"].startswith("\U0001f512 ● Al ")
    assert lines["Bartholomew"].startswith("   ● Bartholomew")

    # Unlocking redraws in place, and a list with no locks spends no cells on the column.
    screen.refresh_locks(frozenset())
    screen.render_body(72)
    al = next(c for c in screen._choices() if getattr(c.value, "name", None) == "Al")
    assert al.title.plain.startswith("● Al ")


async def test_a_locked_contact_is_never_offered_to_the_sweep() -> None:
    """The ranking reads the store's locks, so a locked contact is protected, not a victim."""
    import logging
    from types import SimpleNamespace

    from meshterm.core.contact_score import PROTECT_LOCKED, ContactSignals
    from meshterm.core.models import Contact
    from meshterm.ui.sweep_screen import _rank

    contacts = [
        Contact(name="Kept", public_key="aa" * 32, key_prefix="aa" * 6),
        Contact(name="Loose", public_key="bb" * 32, key_prefix="bb" * 6),
    ]

    class _Repo:
        def contact_signals(self, nodes):  # noqa: ANN001, ANN202
            return {
                n: ContactSignals(node=n, heard_age_days=400.0, known_days=500.0) for n in nodes
            }

        def channel_post_counts(self):  # noqa: ANN202
            return {}

    async def _self_info():  # noqa: ANN202
        return {}

    ctx = SimpleNamespace(
        repo=_Repo(),
        log=logging.getLogger("test.lock"),
        contact_store=SimpleNamespace(locked_keys=lambda dev: frozenset({"aa" * 32})),
        watch_store=None,
        admin_store=None,
        devstate=SimpleNamespace(self_info=_self_info),
    )
    ranked = {s.contact.name: s for s in await _rank(ctx, contacts, "cc" * 32)}
    assert ranked["Kept"].protection == PROTECT_LOCKED
    assert not ranked["Loose"].protected
