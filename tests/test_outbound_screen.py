# SPDX-License-Identifier: Apache-2.0
"""Tests for the Outbound paths pages: the rows, the evidence line, and the measure walk."""

from __future__ import annotations

from pathlib import Path

from meshterm.core.models import Contact, Hop, TraceResult, utcnow
from meshterm.core.outbound_store import OutboundStore
from meshterm.persistence.repository import TracedPath
from meshterm.services.outbound import DeviceIdentity, FirstLink, first_link
from meshterm.services.topology import build_topology
from meshterm.ui.outbound_screen import (
    _count,
    _editor_items,
    _Env,
    _evidence,
    _measured,
    _page,
    _site_actions,
    measure_walk,
)
from meshterm.ui.tui.select import Choice

US = "aaaaaaaaaaaa" + "0" * 52
HUB = Contact(name="Hub", public_key="3d63c6429436" + "0" * 52, key_prefix="3d63c6429436")
FAR = Contact(name="Far", public_key="f2c24f54551e" + "0" * 52, key_prefix="f2c24f54551e")


def _env(walks: int = 3) -> _Env:
    """A session with one trace walk through Hub to Far, repeated."""
    walk = TracedPath(when=utcnow(), hops=[("3d", 9.0), ("f2", -4.0), ("3d", -3.0), (None, 11.0)])
    topo = build_topology(
        self_id=US, contacts=[HUB, FAR], trace_paths=[walk] * walks, packet_paths=[]
    )
    return _Env(DeviceIdentity(US, "Me"), [HUB, FAR], topo, 1)


def _plain(items: list) -> list[str]:
    """The text of each row and separator, as the user reads it."""
    out = []
    for item in items:
        if isinstance(item, Choice):
            text = item.text(200)
        else:
            text = item.title(200) if callable(item.title) else item.title
        out.append(text if isinstance(text, str) else text.plain)
    return out


def test_measure_walk_turns_at_the_last_hop() -> None:
    """Out along the path and back the same way: each link in both directions."""
    assert measure_walk(("a1",)) == ("a1",)
    assert measure_walk(("a1", "b2", "c3")) == ("a1", "b2", "c3", "b2", "a1")


def test_first_link_keeps_the_two_directions_apart() -> None:
    """OUT is what the first hop measured from us. IN is what we measured from it."""
    link = first_link(_env().topology, ("3d63c6429436", "f2c24f54551e"))
    assert (link.out_snr, link.out_count, link.in_snr, link.in_count) == (9.0, 3, 11.0, 3)
    assert first_link(_env().topology, ("9a9a",)) == FirstLink()


def test_the_evidence_line_names_both_directions_or_says_there_is_none() -> None:
    """The line under a path reads ``out … · in … · age``, or says that nothing is known."""
    line = _evidence(FirstLink(9.0, 12, None, 0, utcnow())).plain
    assert line.strip().startswith("out +9 dB ×12 · in -")
    assert "no readings" in _evidence(FirstLink()).plain
    assert [_count(n) for n in (12, 980, 2140, 14600)] == ["12", "980", "2.1k", "14k"]


def test_the_tool_page_marks_the_active_site_and_offers_to_fix_the_device(tmp_path: Path) -> None:
    """Sites first, with ✓ on the active one, then the fixed devices and the fix row."""
    store = OutboundStore(tmp_path / "o.json")
    store.put_site("Home", [("3d63c6429436",)])
    store.put_site("Lab", [])
    store.set_active("Home")
    title, prompt, items = _page(store, _env())
    rows = _plain(items)
    assert (title, prompt) == ("Outbound paths", "Now: site Home")
    assert any(row.startswith("✓ Home") and "1 path" in row for row in rows)
    assert any("Fix Me…" in row for row in rows)
    store.fix_device(US, "Me", [])
    _, prompt, items = _page(store, _env())
    assert prompt.startswith("Now: Me's own paths")
    assert not any("Fix Me…" in row for row in _plain(items))


def test_the_editor_shows_apply_only_with_staged_changes(tmp_path: Path) -> None:
    """An editor ends on its last action until a change is staged, then on Apply over Back."""
    store = OutboundStore(tmp_path / "o.json")
    store.put_site("Home", [("3d63c6429436",)])
    clean = _plain(_editor_items(_env(), [("3d63c6429436",)], _site_actions(store, "Home"), 0))
    assert not any("Apply" in row for row in clean)
    assert any("Use this site" in row for row in clean)
    staged = _plain(_editor_items(_env(), [], _site_actions(store, "Home"), 1))
    assert any("Apply 1 staged change" in row for row in staged)
    assert any("none yet" in row for row in staged)


def test_a_measure_says_who_heard_whom() -> None:
    """Each hop of the reply is one line: the receiver, the sender, and the SNR."""
    walk = measure_walk(("3d63c6429436", "f2c24f54551e"))
    result = TraceResult(
        target="(path)",
        success=True,
        hops=[Hop(0, None, 9.0), Hop(1, None, -4.0), Hop(2, None, -3.0), Hop(3, None, 11.0)],
        round_trip_ms=1500.0,
    )
    lines = _measured(_env(), walk, result).plain.splitlines()
    assert lines == [
        "✓ the trace came back in 1.5 s",
        "Hub heard you at +9.0 dB",
        "Far heard Hub at -4.0 dB",
        "Hub heard Far at -3.0 dB",
        "you heard Hub at +11.0 dB",
    ]
    failed = TraceResult(target="(path)", success=False)
    assert _measured(_env(), walk, failed).plain.startswith("✗ no reply")
