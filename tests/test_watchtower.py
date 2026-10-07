# SPDX-License-Identifier: Apache-2.0
"""Watchtower tests: the storage of the store and the rules of the sentinel.

The tests run the service synchronously through :meth:`note` and :meth:`evaluate`, with a
stub context (the same method as the monitor tests). Thus the tests do not use timers or
hardware.
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
    """A minimal substitute for :class:`~meshterm.context.AppContext` in the rule tests."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = Repository(tmp_path / "wt.db")
        self.watch_store = WatchStore(tmp_path / "watchtower.json")
        self.log = logging.getLogger("test.watchtower")


def _service(tmp_path: Path) -> WatchtowerService:
    ctx = _StubContext(tmp_path)
    service = WatchtowerService(ctx)
    service._known = set()  # ``start()`` gets this from the DB. The history is empty here.
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
    """Watched nodes and their rules survive a new store instance."""
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
    """The store adds alerts with the newest first.

    It limits their number, acknowledges them, and clears them.
    """
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
    assert store.alerts()[0].label == f"n{ALERT_CAP + 9}"  # the newest alert stays at the cap


def test_store_note_heard_updates_and_refreshes_name(tmp_path: Path) -> None:
    """When MeshTerm hears a watched node, the store moves its mark forward.

    The store also takes the new name.
    """
    store = WatchStore(tmp_path / "watchtower.json")
    store.watch(NODE, NODE)
    later = utcnow() + timedelta(minutes=5)
    store.note_heard(NODE, when=later, name="Hilltop-Repeater")
    entry = store.watched()[NODE]
    assert entry.last_heard == later and entry.name == "Hilltop-Repeater"


# --- the silence rule ---------------------------------------------------------------------


def test_silence_fires_once_then_rearms_on_recovery(tmp_path: Path) -> None:
    """The silence rule alarms one time when the node is quiet past the threshold.

    When MeshTerm hears the node again, the rule notes the recovery and arms again.
    """
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.watch(NODE, "Hub", last_seen=utcnow() - timedelta(hours=13))

    service.evaluate()  # the default threshold is 12 h, and the node is past it
    service.evaluate()  # the alarm is latched, so there is no duplicate
    alerts = store.alerts()
    assert [a.kind for a in alerts] == ["silence"]
    assert "12 h" in alerts[0].message

    service.note(_obs())  # the node comes back
    alerts = store.alerts()
    assert [a.kind for a in alerts] == ["recovered", "silence"]
    assert store.watched()[NODE].silent_since is None  # armed again

    service.evaluate()  # the node was heard just now, so it is quiet again only after 12 h more
    assert len(store.alerts()) == 2


def test_silence_respects_off_and_unheard(tmp_path: Path) -> None:
    """A rule that is OFF never alarms, also when the mark is very old."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.watch(NODE, "Hub", last_seen=utcnow() - timedelta(days=30))
    store.set_silence(NODE, OFF)
    service.evaluate()
    assert store.alerts() == []


# --- the SNR rule ---------------------------------------------------------------------


def test_snr_sag_alerts_with_cooldown(tmp_path: Path) -> None:
    """A clear drop in the median SNR gives one alert. The next packets do not give more alerts."""
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

    service.note(_obs(snr=-2.0))  # the SNR is still low, but the cooldown is active
    assert len([a for a in store.alerts() if a.kind == "snr"]) == 1


def test_snr_ignores_packet_rows_and_disabled_watch(tmp_path: Path) -> None:
    """The SNR that a relay measured on a packet keeps the rule quiet.

    A switch that is off also keeps the rule quiet.
    """
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
    """An id that MeshTerm hears for the first time gives one alert.

    Repeats and restarts give none.
    """
    service = _service(tmp_path)
    store = service._ctx.watch_store
    service.note(_obs(node="f7" * 6, name="Newcomer"))
    service.note(_obs(node="f7" * 6, name="Newcomer"))
    news = [a for a in store.alerts() if a.kind == "new-node"]
    assert len(news) == 1 and news[0].label == "Newcomer"
    assert store.known_contains("f7" * 6)  # the store keeps it, so restarts give no alert too


def test_new_node_rule_can_be_switched_off(tmp_path: Path) -> None:
    """If the toggle is off, the store remembers a node that is heard for the first time.

    The store never announces it.
    """
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.set_new_node_alerts(False)
    service.note(_obs(node="c9" * 6))
    assert store.alerts() == []
    assert store.known_contains("c9" * 6)


# --- the screen's rows ---------------------------------------------------------------


def test_store_round_trips_node_type(tmp_path: Path) -> None:
    """The store keeps the advertised type of a starred node. An old entry loads as None."""
    path = tmp_path / "watch.json"
    store = WatchStore(path)
    store.watch("aa" * 6, "Roof", node_type=2)
    store.watch("bb" * 6, "Old-style")  # no type, as in an entry from before the field existed

    reloaded = WatchStore(path)
    assert reloaded.watched()["aa" * 6].node_type == 2
    assert reloaded.watched()["bb" * 6].node_type is None


def test_watched_row_type_glyph_and_hued_name(tmp_path: Path) -> None:
    """A watched row starts with its type glyph, and the name has a hue from the key.

    The glyph keeps its own colour. The name takes the hue that comes from the key of the
    entry. The silence still shows in the tail at the end.
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

    # An old entry (with no stored type) uses the resolver of the contact table.
    legacy = WatchedNode(key="a1" * 6, name="Roof")
    resolved = _watched_row(legacy, lambda key: 2)
    assert resolved.plain.startswith(f"{glyph} ")

    # The ⚠ tail shows the silence. The colour of the glyph never shows it.
    quiet = WatchedNode(key="a1" * 6, name="Roof", node_type=2, silent_since=utcnow())
    assert "⚠ silent" in _watched_row(quiet, lambda key: None).plain


def test_alert_row_hues_the_label_by_resolved_key(tmp_path: Path) -> None:
    """The label of an alert that is not acknowledged takes its hue from the key.

    An acknowledged alert is muted.
    """
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
    """The type glyph that nodes share comes before the node name.

    The glyph has its own colour, and it is muted when the alert is acknowledged.
    """
    from meshterm.core.watch_store import Alert
    from meshterm.ui.watchtower_screen import _alert_lanes
    from meshterm.ui.widgets import DEFAULT_GLYPH, NODE_GLYPHS

    def key_of(label):
        return None

    def type_of(label):
        return 2 if label == "Roof" else None  # Roof advertises itself as a repeater

    glyph, glyph_style = NODE_GLYPHS[2]

    alert = Alert(ident=1, when=utcnow(), kind="silence", label="Roof", message="quiet")
    row = _alert_lanes(alert, key_of, type_of)
    assert f"{glyph} Roof" in row.plain  # the glyph is directly to the left of the name
    gi = row.plain.index(glyph)
    assert any(s.style == glyph_style and s.start <= gi < s.end for s in row.spans)

    # An unknown type, and a kind with no key such as courier, use the glyph of a plain node.
    ghost = Alert(ident=2, when=utcnow(), kind="courier", label="Ghost", message="gave up")
    assert f"{DEFAULT_GLYPH[0]} Ghost" in _alert_lanes(ghost, key_of).plain

    # An acknowledged alert mutes the glyph, with the rest of its history.
    acked = Alert(ident=3, when=utcnow(), kind="silence", label="Roof", message="quiet", acked=True)
    acked_row = _alert_lanes(acked, key_of, type_of)
    gi = acked_row.plain.index(glyph)
    assert any(s.style == "muted" and s.start <= gi < s.end for s in acked_row.spans)


def test_alert_rows_pin_their_lanes_and_scroll_only_the_message() -> None:
    """←→ slide the message of an alert. The marker, age, kind, glyph, and node stay in place.

    The lanes before the ``—`` show which alert this is (JP, 2026-08-10). The user can read
    a long message to its end, and the lanes must not go off the left edge. They are the
    part that already fits. If they scrolled, there would be no gain, and the row would
    lose its identity.
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
    assert lanes.plain.endswith(" — ")  # the lead-in stays with the head that it introduces
    assert row.label.plain[row.hscroll_from :] == alert.message  # and the scroll run is the text


def _word_starts(items: list, words: dict) -> dict:
    """The display cell where the first word of each row starts, by row value.

    The function counts cells and not characters. A ``⭐`` of two cells and a ``✓`` of one
    cell are both one character. A count of characters hid a label that started one column
    too early.
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
    """The action rows share one icon column, across the section headings of the list.

    ``✓`` and ``🗑`` use one cell, and ``⭐`` and ``🔔`` use two. Thus the bulk actions
    started their words one column to the left of *Watch a node…* and the new-node toggle.
    On the PicoCalc the icons are not there, and their padding must go with them.
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
    """The words of the rule dialog share one column, and its values share another.

    This is true on both platforms. ``🕒`` and ``📶`` use two cells, and ``✗`` uses one.
    Thus *Stop watching this node*
    started one column too early. The code once aligned the values with spaces that a
    person typed. Thus the test checks the values too: the values stay in one column, for
    each width of the icon column.
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
