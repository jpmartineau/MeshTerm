# SPDX-License-Identifier: Apache-2.0
"""Dashboard tests: the live screen's state, its chart plumbing, and its data feeds.

The screen is driven headless against fake sessions and canned observations, the same
approach as the live trace/TX screen tests. (The braille chart renderer itself is
covered in ``test_braillechart``.)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from meshterm.core.events import MeshEvent
from meshterm.core.models import Observation, utcnow
from meshterm.persistence.repository import Repository
from meshterm.services.monitor_service import ACTIVITY_BUCKETS
from meshterm.ui.dashboard_screen import DashboardScreen
from tests.conftest import plain as _plain  # THE strip-and-join screen reader


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


def _screen(window=None, histogram=None, kinds=None) -> DashboardScreen:
    screen = DashboardScreen(
        session=_FakeSession(),
        resolve=lambda h: {"a1b2": "Alice", "3d63": "Hub"}.get(h, ""),
        window=list(window or []),
        activity=lambda: tuple(histogram or (0,) * ACTIVITY_BUCKETS),
        activity_flags=lambda: (True,) * ACTIVITY_BUCKETS,
        kind_counts=lambda: dict(kinds or {}),
    )
    screen.note_viewport(48)  # the frame records this before every real paint
    return screen


def _obs(node="a1b2", kind="advert", snr=5.0, rssi=-90.0, age_s=0, **extra) -> Observation:
    return Observation(
        node=node,
        name=node,
        kind=kind,
        snr=snr,
        rssi=rssi,
        observed_at=utcnow() - timedelta(seconds=age_s),
        **extra,
    )


def _stripped(lines: list[str]) -> list[str]:
    """The rendered lines with ANSI escapes removed, for structural assertions."""
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


# --- the screen ----------------------------------------------------------------------


def test_footer_advertises_scroll_only_when_the_overview_overflows() -> None:
    """The scroll keys join the footer only when the body is taller than the viewport."""
    screen = _screen()
    # Fits (the frame recorded a body no taller than the viewport): Esc alone acts.
    screen.note_metrics(total=10, viewport=20)
    assert screen.footer_hint == "Esc back"
    # Overflows: the scroll atom appears, ahead of Esc.
    screen.note_metrics(total=40, viewport=20)
    assert screen.footer_hint == "↑↓ PgUp/PgDn scroll · Esc back"


def test_dashboard_renders_all_three_sections() -> None:
    """Activity, Traffic, and RF health all render from a seeded window.

    (The feed panel moved out to the Live feed tool — see ``test_livefeed``.)
    """
    screen = _screen(
        window=[_obs(), _obs(node="3d63", node_type=2, snr=-2.0)],
        histogram=[3] + [0] * (ACTIVITY_BUCKETS - 1),
        kinds={"packet:ADVERT": 5, "packet:ACK": 1},
    )
    body = _plain(screen.render_body(100))
    assert "Activity" in body and "Traffic" in body and "RF health" in body
    assert "Feed" not in body  # the packet stream lives in the Live feed tool now
    assert "nodes heard" in body and "(1 repeater)" in body
    assert "advert" in body and "Alice" in body  # traffic class + the busiest node


def test_the_activity_aside_names_dot_columns_only_where_braille_has_dots() -> None:
    """The handhelds draw braille solid, so the heading drops ``one minute per dot column``.

    There are no dots there to count, and on 53 columns the words cost the heading its
    line. The desktop keeps them; both keep what the chart counts.
    """
    from meshterm.platforms import PICOCALC, REGULAR, set_platform

    def heading(width: int) -> str:
        body = _plain(_screen(histogram=[1] * ACTIVITY_BUCKETS).render_body(width))
        return next(line for line in body.splitlines() if "Activity" in line)

    set_platform(PICOCALC)
    assert heading(53).strip() == "Activity  ·  every packet heard"
    set_platform(REGULAR)
    assert heading(72).strip() == "Activity  ·  every packet heard · one minute per dot column"


def test_dashboard_activity_chart_reads_newest_right_with_mirrored_scale() -> None:
    """'now' anchors the right edge and the scale marks mirror on both gutters.

    Peak 9 over 3 rows (12 dots): the top gutter's ┤ tick crosses its row's third
    dot (an 11-dot bar → 9·11/12 ≈ 8), so the mark reads 8, not the peak itself.
    """
    screen = _screen(histogram=[9] + [0] * (ACTIVITY_BUCKETS - 1))
    lines = _stripped(screen.render_body(80))
    top = next(line for line in lines if "┤" in line)
    assert top.strip().startswith("8 ┤")
    assert top.rstrip().endswith("├ 8")
    caption = next(line for line in lines if "now" in line)
    assert caption.index("−") < caption.index("now")  # oldest left, newest right
    # The lone newest-minute burst draws against the chart's right gutter, and the
    # left half of the chart is bare flatline.
    chart = [line for line in lines if "┤" in line or "│" in line]
    bottom = chart[-1]
    # The cells between the gutters' borders (a ticked row's are ┤ and ├).
    inner = bottom[re.search("[┤│]", bottom).end() : max(map(bottom.rfind, "├│"))]
    left_half = inner[: len(inner) // 2]
    assert all(ch in (chr(0x2800), chr(0x2800 | 0x40 | 0x80), " ") for ch in left_half)
    assert any(0x2800 <= ord(ch) <= 0x28FF and ord(ch) & 0x3F for ch in chart[0])


def test_dashboard_activity_chart_fills_the_width() -> None:
    """The chart stretches to the render width: wider terminal, more minutes shown.

    Three-digit counts fill the gutters' three cells, so the top row's marks reach both
    edges and its length is the whole width.
    """
    screen = _screen(histogram=[500] * ACTIVITY_BUCKETS)
    for width in (60, 110):
        lines = _stripped(screen.render_body(width))
        top = next(line for line in lines if "┤" in line)
        assert len(top) == width  # gutters + chart consume every cell
    narrow = next(line for line in _stripped(screen.render_body(60)) if "└" in line)
    wide = next(line for line in _stripped(screen.render_body(110)) if "└" in line)
    assert wide.count("─") > narrow.count("─")


def test_dashboard_pulse_drops_heard_only_when_it_wont_fit() -> None:
    """The pulse line keeps ' heard' at full width and sheds it instead of wrapping."""
    from meshterm.ui.tui.render import render_lines

    def pulse_row(width: int) -> str:
        grid = screen._pulse_grid(shown, width)
        return _stripped(render_lines(grid, width))[0]

    screen = _screen(window=[_obs(), _obs(node="3d63", node_type=2)])
    shown = [1] * 120
    roomy = pulse_row(100)
    assert "nodes heard" in roomy
    tight = pulse_row(len(roomy.rstrip()) - 1)
    assert "heard" not in tight and "nodes" in tight


def test_dashboard_traffic_breaks_packets_out_by_payload_class() -> None:
    """Each frame class meters on its own row, glossed and iconed as the live feed does."""
    screen = _screen(
        window=[_obs()],
        kinds={"packet:GRP_TXT": 3, "packet:TRACE": 2, "packet": 1},
    )
    body = _plain(screen.render_body(100))
    assert "chan text" in body and "trace" in body  # glossed, not raw typenames
    assert "GRP_TXT" not in body
    # The class-less remainder keeps a plain "packet" row after the classed ones.
    assert body.index("chan text") < body.index("trace") < body.rindex("packet")


def test_dashboard_traffic_counts_each_frame_once_under_the_feeds_names() -> None:
    """The device's own reports stay out: an advert is its ADVERT frame, counted once.

    The monitor tallies the advert *event* and the RX-logged frame it arrived in, and a
    decoded message beside its ``chan text`` frame; counting both lists one packet twice.
    The frame's row takes the live feed's name — ``advert``, never a "raw" variant.
    """
    from meshterm.ui.packet_viewer import payload_marks

    kinds = {
        "advert": 40,
        "telemetry": 9,
        "message": 7,
        "ack": 30,
        "packet:ADVERT": 2,
        "packet:ACK": 1,
    }
    lines = _plain(_screen(window=[_obs()], kinds=kinds).render_body(100)).splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Traffic"))
    rows = lines[start + 1 : lines.index("", start)]
    assert [r.split()[1:3] for r in rows] == [["advert", "2"], ["ack", "1"]]
    assert not any("raw" in r or "telemetry" in r or "message" in r for r in rows)
    assert payload_marks("ADVERT")[1] == "advert" and payload_marks(None)[1] == "packet"


def test_dashboard_live_observations_land_in_the_window() -> None:
    """A hub observation folds into the trailing window and repaints the charts."""
    screen = _screen()
    screen.on_event(MeshEvent.observation_event(_obs(snr=7.5)))
    body = _plain(screen.render_body(100))
    assert "+7.5 dB median" in body  # the RF section now has a reception to describe
    assert screen._session.repaints >= 1


def test_dashboard_prunes_the_window() -> None:
    """Observations older than the window drop out of the statistics."""
    stale = _obs(age_s=3 * 3600, snr=-12.0)
    screen = _screen(window=[stale])
    body = _plain(screen.render_body(100))
    assert "no receptions in the window yet" in body  # stats pruned the stale row


def test_dashboard_radio_rows_render_device_stats() -> None:
    """The Device-info dynamic numbers (noise floor, airtime, battery) fold in."""
    screen = _screen(window=[_obs()])
    screen.stats = {
        "noise_floor": -104,
        "last_rssi": -95,
        "last_snr": 5.5,
        "tx_air_secs": 12,
        "rx_air_secs": 340,
    }
    screen.battery = {"level": 4010}
    body = _plain(screen.render_body(100))
    assert "-104 dBm noise floor" in body
    assert "TX 12 s · RX 340 s" in body
    assert "4.01 V" in body


def test_repository_kind_counts_bucket_packets_by_payload_class(tmp_path: Path) -> None:
    """Stored packet frames with a payload class seed as ``packet:<TYPENAME>`` buckets."""
    repo = Repository(tmp_path / "kinds.db")
    run = repo.start_run("monitor", {}, None)
    repo.record_observation(run, _obs(node="a", kind="advert"))
    repo.record_observation(run, _obs(node="", kind="packet", raw={"payload_typename": "GRP_TXT"}))
    repo.record_observation(run, _obs(node="", kind="packet", raw={"payload_typename": "GRP_TXT"}))
    repo.record_observation(run, _obs(node="", kind="packet"))  # class-less frame

    counts = repo.kind_counts()
    assert counts["advert"] == 1
    assert counts["packet:GRP_TXT"] == 2
    assert counts["packet"] == 1  # only the class-less frame stays a bare packet
    repo.close()


def test_monitor_tallies_live_packets_by_payload_class(tmp_path: Path) -> None:
    """A live classed packet observation lands in its ``packet:<TYPENAME>`` bucket."""
    from meshterm.services.monitor_service import MonitorService

    monitor = MonitorService.__new__(MonitorService)
    monitor._activity = {}
    monitor._activity_stamp = 0
    monitor._kind_counts = {}
    monitor._count_packet(
        MeshEvent.observation_event(_obs(node="", kind="packet", raw={"payload_typename": "TRACE"}))
    )
    monitor._count_packet(MeshEvent.observation_event(_obs(kind="advert")))
    assert monitor._kind_counts == {"packet:TRACE": 1, "advert": 1}


# --- the repository window -----------------------------------------------------------


def test_recent_observations_windows_and_orders(tmp_path: Path) -> None:
    """The seed query returns only the window, oldest first, hydrated with kinds."""
    repo = Repository(tmp_path / "dash.db")
    run = repo.start_run("monitor", {}, None)
    repo.record_observation(run, _obs(node="old", age_s=3 * 3600))
    repo.record_observation(run, _obs(node="mid", kind="packet", age_s=600, path="3d63"))
    repo.record_observation(run, _obs(node="new", age_s=5))

    window = repo.recent_observations(since=utcnow() - timedelta(hours=2))
    assert [o.node for o in window] == ["mid", "new"]
    assert window[0].kind == "packet" and window[0].path == "3d63"
    repo.close()


def test_recent_observations_rehydrate_channel_text_for_decryption(tmp_path: Path) -> None:
    """A stored GRP_TXT frame comes back with the fields the packet viewer decrypts from."""
    from Crypto.Cipher import AES
    from Crypto.Hash import HMAC, SHA256

    from meshterm.core.channels import channel_hash, derive_secret
    from meshterm.ui.packet_viewer import PacketEntry, PacketViewer

    name, secret = "#general", derive_secret("#general")
    plain = (0).to_bytes(4, "little") + bytes([0]) + b"hi from history"
    plain += b"\x00" * (-len(plain) % 16)
    crypted = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = HMAC.new(secret, digestmod=SHA256)
    mac.update(crypted)
    raw = {
        "payload_typename": "GRP_TXT",
        "chan_hash": channel_hash(secret),
        "cipher_mac": mac.digest()[:2].hex(),
        "crypted": crypted.hex(),
    }

    repo = Repository(tmp_path / "chan.db")
    run = repo.start_run("monitor", {}, None)
    repo.record_observation(run, _obs(node="src", kind="packet", path="3d63", raw=raw))
    repo.record_observation(run, _obs(node="adv", kind="advert"))  # a plain advert nearby

    window = {o.node: o for o in repo.recent_observations(since=utcnow() - timedelta(hours=2))}
    stored = window["src"]
    assert stored.raw is not None and stored.raw["chan_hash"] == channel_hash(secret)
    assert window["adv"].raw is None  # nothing to rebuild from → no raw, as before

    # The stored frame decrypts straight out of history — the whole point of persisting it.
    viewer = PacketViewer(
        [PacketEntry.from_observation(stored)],
        0,
        resolve=lambda h: "",
        channels=[(name, secret)],
    )
    body = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in viewer.render_body(80))
    assert "#general" in body and "hi from history" in body
    repo.close()


def test_recent_observations_rehydrate_the_packet_payload_class(tmp_path: Path) -> None:
    """A stored ``packet`` comes back carrying its payload class, so a list can name it."""
    repo = Repository(tmp_path / "cls.db")
    run = repo.start_run("monitor", {}, None)
    repo.record_observation(
        run,
        _obs(node="", kind="packet", path="3d63", raw={"payload_typename": "TRACE"}),
    )
    repo.record_observation(run, _obs(node="a1b2", kind="advert"))  # a plain advert nearby

    recent = repo.recent_observations(since=utcnow() - timedelta(hours=2))
    window = {o.node or "": o for o in recent}
    assert window[""].raw is not None and window[""].raw["payload_typename"] == "TRACE"
    assert window["a1b2"].raw is None  # an advert keeps no packet raw, as before
    repo.close()


def test_rhythm_activity_buckets_by_minute_of_day(tmp_path: Path) -> None:
    """Observations fall into 1440 minute-of-day slots (``HH * 60 + MM``).

    The slots are local time-of-day, so the observations are stamped at local
    wall-clock instants (naive → ``.astimezone()``) — the slot index then matches
    each ``HH:MM`` directly, in any runner zone. A minute is the base grid every
    rhythm slice width (1/5/10/15/20/30/60 min) folds from without re-querying.
    """
    repo = Repository(tmp_path / "q.db")
    run = repo.start_run("monitor", {}, None)

    def at(hh: int, mm: int) -> Observation:
        return Observation(
            node="n",
            name="n",
            kind="advert",
            observed_at=datetime(2026, 7, 8, hh, mm).astimezone(),
        )

    for hh, mm in [(0, 0), (0, 44), (17, 55), (17, 55)]:
        repo.record_observation(run, at(hh, mm))
    slots = repo.rhythm_activity()
    assert len(slots) == 1440
    assert slots[0] == 1  # 00:00 → slot 0
    assert slots[44] == 1  # 00:44 → slot 44
    assert slots[17 * 60 + 55] == 2  # both 17:55 stamps share one minute slot
    assert sum(slots) == 4
    repo.close()


# --- what the once-a-second repaint is allowed to redo --------------------------------


def test_the_window_digest_only_runs_when_the_window_moved() -> None:
    """The screen repaints every second; the aggregates are a pure function of the deque.

    On a mesh quiet for a beat, every one of those frames re-walked up to four thousand
    observations to arrive at the numbers it already had. The window changes in exactly two
    places — an arriving observation and an expiring one — and only those may cost a pass.
    """
    screen = _screen(window=[_obs(node=f"n{i:03d}", age_s=i) for i in range(200)])
    passes = [0]
    inner = type(screen)._digest_window

    def counted(self) -> None:
        before = self._digest_rev
        inner(self)
        if before != self._digest_rev:
            passes[0] += 1

    screen._digest_window = counted.__get__(screen)

    screen.render_body(53)
    assert passes[0] == 1
    screen.render_body(53)
    screen.render_body(53)
    assert passes[0] == 1, "an idle repaint must not re-walk the window"

    screen.on_event(MeshEvent.observation_event(_obs(node="new1")))
    screen.render_body(53)
    assert passes[0] == 2, "an arriving observation must refresh the aggregates"


def test_a_new_observation_still_reaches_the_numbers_on_the_next_paint() -> None:
    """The memo must not freeze the screen: a heard node has to show up in the tallies."""
    screen = _screen(window=[_obs(node="a1b2")])
    screen.render_body(53)
    assert screen._win_nodes == {"a1b2"}

    screen.on_event(MeshEvent.observation_event(_obs(node="3d63")))
    screen.render_body(53)
    assert screen._win_nodes == {"a1b2", "3d63"}


def test_the_activity_chart_is_redrawn_only_when_its_picture_changes() -> None:
    """Rasterizing the braille rows is the priciest part of this paint — do it once."""
    histogram = [0] * ACTIVITY_BUCKETS
    histogram[0] = 5
    screen = _screen(histogram=histogram)

    screen.render_body(53)
    first = screen._chart_memo
    assert first is not None
    screen.render_body(53)
    assert screen._chart_memo is first, "an identical histogram must reuse the chart"

    histogram[0] = 40  # a packet lands: the newest column grows
    screen.render_body(53)
    assert screen._chart_memo is not first
    assert screen._chart_memo[0] != first[0]


def test_the_chart_is_redrawn_when_the_terminal_width_changes() -> None:
    """The chart is sized to the terminal, so the memo key has to carry the width."""
    screen = _screen(histogram=[3] * ACTIVITY_BUCKETS)
    screen.render_body(53)
    narrow = screen._chart_memo
    screen.render_body(100)
    assert screen._chart_memo is not narrow


def _flood(region: str | None, typename: str = "GRP_TXT", snr: float = 4.0) -> Observation:
    """A packet frame whose raw payload names its scope the way ``_fake_scope_of`` reads it.

    ``region`` is a region name, ``""`` for an unscoped flood, ``"?"`` for a scope no known
    name reproduces, and ``None`` for a direct frame, which has no scope at all.
    """
    raw = {"payload_typename": typename, "fake_scope": region}
    return _obs(node=None, kind="packet", snr=snr, raw=raw)


def _fake_scope_of(raw):  # noqa: ANN001, ANN202 - the RegionStore.scope_of shape
    from meshterm.core.regions import UNSCOPED, Scope

    region = (raw or {}).get("fake_scope")
    if region is None:
        return None
    if region == "":
        return UNSCOPED
    if region == "?":
        return Scope("unknown", None, "3fa1")
    return Scope("scoped", region, "beef")


def _scoped_screen(window) -> DashboardScreen:
    screen = DashboardScreen(
        session=_FakeSession(),
        resolve=lambda h: "",
        window=list(window),
        activity=lambda: (0,) * ACTIVITY_BUCKETS,
        activity_flags=lambda: (True,) * ACTIVITY_BUCKETS,
        kind_counts=lambda: {"packet:GRP_TXT": 50, "packet:ACK": 9},
        scope_of=_fake_scope_of,
    )
    screen.note_viewport(48)
    return screen


def test_the_scope_cycle_steps_through_every_scope_heard_and_back_to_all() -> None:
    """``s`` cycles all → unscoped → regions A–Z → unknown → all, retitling each view.

    Direct frames add no view: they have no scope, so they are counted only under all.
    """
    screen = _scoped_screen(
        [_flood("yul"), _flood(""), _flood("?"), _flood("mtl"), _flood(None, "TEXT_MSG")]
    )
    assert screen.title == "Dashboard — mesh overview" and "s scope" in screen.footer_hint
    seen = []
    for _ in range(5):
        screen.handle("text", "s")
        seen.append(screen.title.removeprefix("Dashboard — mesh overview"))
    assert seen == [
        " · unscoped",
        " · scope mtl",
        " · scope yul",
        " · unknown scope",
        "",
    ]
    screen.handle("scope")  # the F3 chip's action is the same cycle
    assert screen.short_title == "Dashboard · unscoped"


def test_a_scope_view_narrows_traffic_and_reception_to_its_floods() -> None:
    """Traffic and RF read the view's frames; the all view keeps the stored tallies."""
    screen = _scoped_screen(
        [
            _flood("yul", "GRP_TXT", snr=8.0),
            _flood("yul", "TRACE", snr=8.0),
            _flood("mtl", "GRP_TXT", snr=-9.0),
            _flood(None, "ACK", snr=-9.0),
        ]
    )
    screen.handle("text", "s")  # mtl (no unscoped floods in this window)
    screen.handle("text", "s")  # yul
    assert screen.title.endswith("scope yul")
    body = _plain(screen.render_body(100))
    assert "floods in the last 2 h" in body
    traffic = body[body.index("Traffic") : body.index("RF health")]
    assert "chan text" in traffic and "trace" in traffic and " ack " not in traffic
    assert "+8.0 dB median" in body  # mtl's -9 and the direct ACK's stay out
    assert "nodes" not in body and "busiest" not in body  # adverts carry no scope

    screen.handle("text", "s")  # back to all
    body = _plain(screen.render_body(100))
    assert "stored history + live" in body and "50" in body


def test_no_scope_view_is_offered_without_a_flood_to_narrow_to() -> None:
    """A window of direct frames only offers nothing to cycle: no hint atom, no chip."""
    screen = _scoped_screen([_flood(None, "TEXT_MSG")])
    assert "scope" not in screen.footer_hint
    assert screen.picocalc_lane[2] is None or not getattr(screen.picocalc_lane[2], "label", "")
    screen.handle("text", "s")
    assert screen.title == "Dashboard — mesh overview"
