# SPDX-License-Identifier: Apache-2.0
"""Watchtower tests: the store's persistence and the sentinel's rules.

The service is driven synchronously through :meth:`note`/:meth:`evaluate` against a
stub context (the monitor tests' approach), so no timers or hardware are involved.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from meshterm.core.models import Observation, utcnow
from meshterm.core.watch_store import ALERT_CAP, OFF, WatchStore
from meshterm.persistence.repository import Repository
from meshterm.services.watchtower import SNR_WINDOW, WatchtowerService

NODE = "3d" * 6


class _StubContext:
    """Minimal stand-in for :class:`~meshterm.context.AppContext` for rule tests."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = Repository(tmp_path / "wt.db")
        self.watch_store = WatchStore(tmp_path / "watchtower.json")
        self.log = logging.getLogger("test.watchtower")


def _service(tmp_path: Path) -> WatchtowerService:
    ctx = _StubContext(tmp_path)
    service = WatchtowerService(ctx)
    service._known = set()  # what start() seeds from the DB; empty history here
    return service


def _obs(node=NODE, name="Hub", snr=None, age_s=0, kind="advert") -> Observation:
    return Observation(
        node=node,
        name=name,
        kind=kind,
        snr=snr,
        observed_at=utcnow() - timedelta(seconds=age_s),
    )


# --- the store --------------------------------------------------------------------------


def test_store_watch_round_trips_and_persists(tmp_path: Path) -> None:
    """Watched nodes and their rules survive a fresh store instance."""
    path = tmp_path / "watchtower.json"
    store = WatchStore(path)
    seen = utcnow() - timedelta(hours=2)
    store.watch(NODE, "Hub", last_seen=seen)
    store.set_silence(NODE, 24)
    store.set_snr_watch(NODE, False)

    again = WatchStore(path)
    entry = again.watched()[NODE]
    assert entry.name == "Hub" and entry.silence_hours == 24 and not entry.snr_watch
    assert entry.last_heard == seen
    again.unwatch(NODE)
    assert not WatchStore(path).watched()


def test_store_alert_log_caps_acks_and_clears(tmp_path: Path) -> None:
    """Alerts append newest-first, cap, acknowledge, and clear."""
    store = WatchStore(tmp_path / "watchtower.json")
    first = store.add_alert("silence", "Hub", "quiet")
    store.add_alert("snr", "Hub", "sagging")
    assert [a.kind for a in store.alerts()] == ["snr", "silence"]
    assert store.unacked_count() == 2

    store.ack(first.ident)
    assert store.unacked_count() == 1
    store.ack_all()
    assert store.unacked_count() == 0
    store.clear_acked()
    assert store.alerts() == []

    for i in range(ALERT_CAP + 10):
        store.add_alert("new-node", f"n{i}", "hi")
    assert len(store.alerts()) == ALERT_CAP
    assert store.alerts()[0].label == f"n{ALERT_CAP + 9}"  # newest survives the cap


def test_store_note_heard_updates_and_refreshes_name(tmp_path: Path) -> None:
    """Hearing a watched node advances its mark and adopts a fresh name."""
    store = WatchStore(tmp_path / "watchtower.json")
    store.watch(NODE, NODE)
    later = utcnow() + timedelta(minutes=5)
    store.note_heard(NODE, when=later, name="Hilltop-Repeater")
    entry = store.watched()[NODE]
    assert entry.last_heard == later and entry.name == "Hilltop-Repeater"


# --- the silence rule ---------------------------------------------------------------------


def test_silence_fires_once_then_rearms_on_recovery(tmp_path: Path) -> None:
    """Quiet past the threshold alarms once; hearing again notes the recovery."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.watch(NODE, "Hub", last_seen=utcnow() - timedelta(hours=13))

    service.evaluate()  # default threshold is 12 h — already past it
    service.evaluate()  # latched: no duplicate
    alerts = store.alerts()
    assert [a.kind for a in alerts] == ["silence"]
    assert "12 h" in alerts[0].message

    service.note(_obs())  # the node returns
    alerts = store.alerts()
    assert [a.kind for a in alerts] == ["recovered", "silence"]
    assert store.watched()[NODE].silent_since is None  # re-armed

    service.evaluate()  # freshly heard: quiet again only after another 12 h
    assert len(store.alerts()) == 2


def test_silence_respects_off_and_unheard(tmp_path: Path) -> None:
    """An OFF rule never alarms, however stale the mark."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.watch(NODE, "Hub", last_seen=utcnow() - timedelta(days=30))
    store.set_silence(NODE, OFF)
    service.evaluate()
    assert store.alerts() == []


# --- the SNR rule ---------------------------------------------------------------------


def test_snr_sag_alerts_with_cooldown(tmp_path: Path) -> None:
    """A clear drop in median SNR alerts once, not on every subsequent packet."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.watch(NODE, "Hub")

    for _ in range(SNR_WINDOW):
        service.note(_obs(snr=8.0))
    for _ in range(SNR_WINDOW):
        service.note(_obs(snr=-2.0))
    sags = [a for a in store.alerts() if a.kind == "snr"]
    assert len(sags) == 1
    assert "+8.0" in sags[0].message and "-2.0" in sags[0].message

    service.note(_obs(snr=-2.0))  # still sagging, but inside the cooldown
    assert len([a for a in store.alerts() if a.kind == "snr"]) == 1


def test_snr_ignores_packet_rows_and_disabled_watch(tmp_path: Path) -> None:
    """Relay-measured packet SNR and an off switch both keep the rule quiet."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.watch(NODE, "Hub")
    store.set_snr_watch(NODE, False)
    for snr in (9.0,) * SNR_WINDOW + (-9.0,) * SNR_WINDOW:
        service.note(_obs(snr=snr))
    assert [a for a in store.alerts() if a.kind == "snr"] == []

    store.set_snr_watch(NODE, True)
    for snr in (9.0,) * SNR_WINDOW + (-9.0,) * SNR_WINDOW:
        service.note(_obs(snr=snr, kind="packet"))
    assert [a for a in store.alerts() if a.kind == "snr"] == []


# --- the new-node rule ------------------------------------------------------------------


def test_new_node_announced_once_ever(tmp_path: Path) -> None:
    """A first-ever id alerts once; repeats and restarts stay quiet."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    service.note(_obs(node="f7" * 6, name="Newcomer"))
    service.note(_obs(node="f7" * 6, name="Newcomer"))
    news = [a for a in store.alerts() if a.kind == "new-node"]
    assert len(news) == 1 and news[0].label == "Newcomer"
    assert store.known_contains("f7" * 6)  # persisted: restarts stay quiet too


def test_new_node_rule_can_be_switched_off(tmp_path: Path) -> None:
    """With the toggle off, first sightings are remembered but never announced."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.set_new_node_alerts(False)
    service.note(_obs(node="c9" * 6))
    assert store.alerts() == []
    assert store.known_contains("c9" * 6)


# --- the screen's rows ---------------------------------------------------------------


def test_store_round_trips_node_type(tmp_path: Path) -> None:
    """A starred node's advertised type persists; legacy entries load as None."""
    path = tmp_path / "watch.json"
    store = WatchStore(path)
    store.watch("aa" * 6, "Roof", node_type=2)
    store.watch("bb" * 6, "Old-style")  # no type, like a pre-field entry

    reloaded = WatchStore(path)
    assert reloaded.watched()["aa" * 6].node_type == 2
    assert reloaded.watched()["bb" * 6].node_type is None


def test_watched_row_type_glyph_and_hued_name(tmp_path: Path) -> None:
    """A watched row leads with its type glyph and hues the name by key.

    The glyph keeps its own colour and the name takes the entry's key-derived hue;
    silence still reads from the trailing tail.
    """
    from meshterm.core.watch_store import WatchedNode
    from meshterm.ui.theme import name_style
    from meshterm.ui.watchtower_screen import _watched_row
    from meshterm.ui.widgets import NODE_GLYPHS

    entry = WatchedNode(key="a1" * 6, name="Roof", node_type=2, last_heard=utcnow())
    row = _watched_row(entry, lambda key: None)
    glyph, glyph_style = NODE_GLYPHS[2]
    assert row.plain.startswith(f"{glyph} Roof")
    assert any(s.style == glyph_style and s.start == 0 for s in row.spans)
    name_at = row.plain.index("Roof")
    assert any(
        s.style == name_style("Roof", "a1" * 6) and s.start <= name_at < s.end for s in row.spans
    )

    # A legacy entry (no stored type) falls back to the contact table's resolver.
    legacy = WatchedNode(key="a1" * 6, name="Roof")
    resolved = _watched_row(legacy, lambda key: 2)
    assert resolved.plain.startswith(f"{glyph} ")

    # Silence is signalled by the ⚠ tail, never the glyph colour.
    quiet = WatchedNode(key="a1" * 6, name="Roof", node_type=2, silent_since=utcnow())
    assert "⚠ silent" in _watched_row(quiet, lambda key: None).plain


def test_alert_row_hues_the_label_by_resolved_key(tmp_path: Path) -> None:
    """An unacked alert's label takes its key-derived hue; acked recedes to muted."""
    from meshterm.core.watch_store import Alert
    from meshterm.ui.theme import name_style
    from meshterm.ui.watchtower_screen import _alert_lanes

    def key_of(label):
        return "d4" * 6 if label == "Roof" else None

    alert = Alert(ident=1, when=utcnow(), kind="silence", label="Roof", message="quiet")
    row = _alert_lanes(alert, key_of)
    at = row.plain.index("Roof")
    assert any(s.style == name_style("Roof", "d4" * 6) and s.start <= at < s.end for s in row.spans)

    acked = Alert(ident=2, when=utcnow(), kind="silence", label="Roof", message="quiet", acked=True)
    acked_row = _alert_lanes(acked, key_of)
    at = acked_row.plain.index("Roof")
    assert any(s.style == "muted" and s.start <= at < s.end for s in acked_row.spans)


def test_alert_row_leads_node_name_with_type_glyph(tmp_path: Path) -> None:
    """The node name is preceded by its shared type glyph (own colour, muted when acked)."""
    from meshterm.core.watch_store import Alert
    from meshterm.ui.watchtower_screen import _alert_lanes
    from meshterm.ui.widgets import DEFAULT_GLYPH, NODE_GLYPHS

    def key_of(label):
        return None

    def type_of(label):
        return 2 if label == "Roof" else None  # Roof advertises as a repeater

    glyph, glyph_style = NODE_GLYPHS[2]

    alert = Alert(ident=1, when=utcnow(), kind="silence", label="Roof", message="quiet")
    row = _alert_lanes(alert, key_of, type_of)
    assert f"{glyph} Roof" in row.plain  # glyph sits immediately left of the name
    gi = row.plain.index(glyph)
    assert any(s.style == glyph_style and s.start <= gi < s.end for s in row.spans)

    # Unknown type (and a keyless kind like courier) falls back to the plain-node glyph.
    ghost = Alert(ident=2, when=utcnow(), kind="courier", label="Ghost", message="gave up")
    assert f"{DEFAULT_GLYPH[0]} Ghost" in _alert_lanes(ghost, key_of).plain

    # An acked alert mutes the glyph with the rest of its history.
    acked = Alert(ident=3, when=utcnow(), kind="silence", label="Roof", message="quiet", acked=True)
    acked_row = _alert_lanes(acked, key_of, type_of)
    gi = acked_row.plain.index(glyph)
    assert any(s.style == "muted" and s.start <= gi < s.end for s in acked_row.spans)


def test_alert_rows_pin_their_lanes_and_scroll_only_the_message() -> None:
    """←→ slide an alert's *message*; the marker, age, kind, glyph and node hold still.

    The lanes in front of the ``—`` are which alert this is (JP, 2026-08-10). Reading a
    long message to its end is no reason to lose them off the left edge — they are the part
    that already fits, so scrolling them buys nothing and costs the row its identity.
    """
    from meshterm.core.watch_store import Alert
    from meshterm.ui.tui import Choice
    from meshterm.ui.watchtower_screen import _alert_lanes, _menu_items

    alert = Alert(
        ident=1,
        when=utcnow(),
        kind="silence",
        label="Roof",
        message="nothing heard for 6 h " + "and counting " * 6,
    )
    items = _menu_items([alert], {}, False)
    row = next(it for it in items if isinstance(it, Choice) and it.value == ("ack", alert.ident))

    lanes = _alert_lanes(alert, lambda label: None)
    assert row.hscroll_from == lanes.cell_len
    assert row.label.plain.startswith(lanes.plain)
    assert lanes.plain.endswith(" — ")  # the lead-in stays with the head it introduces
    assert row.label.plain[row.hscroll_from :] == alert.message  # …and the run is the message


def _word_starts(items: list, words: dict) -> dict:
    """The display cell each row's first word starts in, by row value.

    Cells, not characters: a two-cell ``⭐`` and a one-cell ``✓`` are both one character, so
    counting characters is exactly what hid a label starting a column early.
    """
    from rich.cells import cell_len

    from meshterm.ui.tui import Choice

    starts = {}
    for item in items:
        if isinstance(item, Choice) and item.value in words:
            plain = item.label.plain
            starts[item.value] = cell_len(plain[: plain.index(words[item.value])])
    assert starts.keys() == words.keys(), "every row the test names is on the screen"
    return starts


def test_watchtower_actions_start_every_label_in_the_same_cell() -> None:
    """The action rows share one icon column, across the list's section headings.

    ``✓`` and ``🗑`` draw one cell and ``⭐`` and ``🔔`` two, so the bulk actions started
    their words a column left of *Watch a node…* and the new-node toggle. On the PicoCalc
    the icons go and must take their padding with them.
    """
    from rich.cells import cell_len

    from meshterm.core.watch_store import Alert
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui.watchtower_screen import _ACK_ALL, _CLEAR, _TOGGLE_NEW, _WATCH, _menu_items

    assert {cell_len("✓"), cell_len("⭐")} == {1, 2}, "a list of one icon width proves nothing"
    alerts = [
        Alert(ident=1, when=utcnow(), kind="silence", label="Roof", message="quiet"),
        Alert(ident=2, when=utcnow(), kind="snr", label="Roof", message="sag", acked=True),
    ]
    words = {
        _ACK_ALL: "Acknowledge",
        _CLEAR: "Clear",
        _WATCH: "Watch",
        _TOGGLE_NEW: "New-node",
    }
    assert set(_word_starts(_menu_items(alerts, {}, True), words).values()) == {3}
    try:
        set_platform(PICOCALC_LYRA)
        assert set(_word_starts(_menu_items(alerts, {}, True), words).values()) == {0}
    finally:
        set_platform(REGULAR)


def test_node_rules_start_every_label_and_value_in_the_same_cell() -> None:
    """The rule popover's words share one column, and its values another, on both platforms.

    ``🕒`` and ``📶`` draw two cells and ``✗`` one, so *Stop watching this node* started a
    column early. The values used to be lined up with hand-typed spaces, so they are pinned
    here too: whatever the icon column measures, the values stay in one column.
    """
    from meshterm.core.watch_store import WatchedNode
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui.watchtower_screen import _rule_items

    entry = WatchedNode(key="a1" * 6, name="Roof", silence_hours=12, snr_watch=True)
    words = {"silence": "Silence", "snr": "SNR", "unwatch": "Stop"}
    values = {"silence": "after 12 h", "snr": "on"}
    try:
        for platform, start in ((REGULAR, 3), (PICOCALC_LYRA, 0)):
            set_platform(platform)
            items = _rule_items(entry)
            assert set(_word_starts(items, words).values()) == {start}
            assert len(set(_word_starts(items, values).values())) == 1
    finally:
        set_platform(REGULAR)
