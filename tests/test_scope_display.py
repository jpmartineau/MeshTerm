# SPDX-License-Identifier: Apache-2.0
"""Where a flood's scope is shown: the packet viewer, the live feed, message paths, monitor.

The scope itself — the maths, the store, the history columns — is pinned in
``test_regions.py``; these tests are about the surfaces that state it, and the one rule
they all share: a flood says which region it was sent into (or that it was unscoped, or
scoped to a region nobody here has named), and a direct frame says nothing at all.
"""

from __future__ import annotations

import io
import json
import re
from datetime import timedelta
from pathlib import Path

from rich.cells import cell_len

from meshterm.core.models import ChatMessage, Observation, utcnow
from meshterm.core.region_store import RegionStore
from meshterm.core.regions import (
    UNSCOPED,
    Scope,
    frame_scope,
    region_key,
    scope_body,
    transport_code,
)
from meshterm.persistence.repository import Repository
from meshterm.services.message_paths import (
    Arrival,
    channel_arrivals,
    collapse,
    message_scope,
)
from meshterm.tools.monitor import _live_packets
from meshterm.ui import script
from meshterm.ui.livefeed_screen import LiveFeedScreen
from meshterm.ui.message_paths_screen import MessagePathsScreen
from meshterm.ui.packet_viewer import PacketEntry, PacketViewer
from meshterm.ui.renderers import JsonRenderer, PlainRenderer
from tests.conftest import plain as _plain
from tests.test_message_paths import SECRET, _grp_txt_raw

#: An arbitrary channel-text payload to scope (the code is over it, not over its meaning).
_PAYLOAD = bytes.fromhex("a71c2d00112233445566778899aabbccddeeff")


def _scoped(region: str, payload: bytes = _PAYLOAD, **extra) -> dict:
    """An RX-log frame's raw payload for a channel text flooded under ``region``."""
    code = transport_code(region_key(region), scope_body(5, payload))
    return {
        "route_typename": "TC_FLOOD",
        "payload_type": 5,
        "payload_typename": "GRP_TXT",
        "pkt_payload": payload,
        "transport_code": code.to_bytes(2, "little").hex() + "0000",
        **extra,
    }


def _code(region: str) -> str:
    """The 4-hex code a :func:`_scoped` frame carries."""
    return f"{transport_code(region_key(region), scope_body(5, _PAYLOAD)):04x}"


_FLOOD = {"route_typename": "FLOOD", "payload_typename": "GRP_TXT", "chan_hash": "a7"}
_DIRECT = {"route_typename": "DIRECT", "payload_typename": "TEXT_MSG", "dest_hash": "a1"}


def _knows(*names: str):  # noqa: ANN202 - a scope reader over a fixed set of names
    """A ``scope_of`` that knows exactly ``names`` — the region store's answer, in small."""
    return lambda raw: frame_scope(raw, names) if isinstance(raw, dict) else None


def _stripped(lines: list[str]) -> list[str]:
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


def _col(line: str, needle: str) -> int:
    """The display column (cells, not characters — the class icon is two) of ``needle``."""
    return cell_len(line[: line.index(needle)])


# -- the packet viewer -------------------------------------------------------------------


def _route_row(raw: dict, scope_of=None) -> str:  # noqa: ANN001
    """The viewer's ``route`` row for one frame, as plain text."""
    entry = PacketEntry(when=utcnow(), kind="packet", path="3d", raw=raw)
    body = _stripped(
        PacketViewer([entry], 0, resolve=lambda h: "", scope_of=scope_of).render_body(80)
    )
    return next(line for line in body if line.startswith("route")).rstrip()


def test_the_viewer_names_the_region_a_flood_was_scoped_to() -> None:
    """``tc flood`` said *how* and never *where*; the row now says flood, then the scope."""
    assert _route_row(_scoped("harbour"), _knows("harbour")).split(None, 1)[1] == (
        "flood · scope harbour"
    )


def test_the_viewer_shows_an_unnamed_scopes_code_and_a_plain_flood_as_unscoped() -> None:
    """A scope no known name reproduces keeps its code; a plain flood reads unscoped."""
    assert _route_row(_scoped("elsewhere"), _knows("harbour")).endswith(
        f"flood · unknown scope {_code('elsewhere')}"
    )
    assert _route_row(_FLOOD, _knows("harbour")).endswith("flood · unscoped")


def test_the_viewer_without_a_store_still_tells_scoped_from_unscoped() -> None:
    """No names at hand is less of the truth, never a wrong one: the code stands in."""
    assert _route_row(_scoped("harbour")).endswith(f"flood · unknown scope {_code('harbour')}")
    assert _route_row(_FLOOD).endswith("flood · unscoped")


def test_the_viewer_never_gives_a_direct_frame_a_scope() -> None:
    """A repeater never region-filters a direct packet, so its route row is the word alone."""
    assert _route_row(_DIRECT, _knows("harbour")).split() == ["route", "direct"]
    assert _route_row({**_DIRECT, "route_typename": "TC_DIRECT"}).split() == [
        "route",
        "tc",
        "direct",
    ]


def test_the_viewer_keeps_the_scope_body_out_of_the_raw_dump(tmp_path: Path) -> None:
    """The restored HMAC input is plumbing the route row already spoke for."""
    raw = _scoped("harbour")
    restored = {k: v for k, v in raw.items() if k not in ("payload_type", "pkt_payload")}
    restored["scope_body"] = scope_body(5, _PAYLOAD).hex()
    body = _plain(
        PacketViewer(
            [PacketEntry(when=utcnow(), kind="packet", path="", raw=restored)],
            0,
            resolve=lambda h: "",
            scope_of=_knows("harbour"),
        ).render_body(80)
    )
    assert "scope harbour" in body  # a replayed frame resolves exactly as a live one
    assert "scope_body" not in body


def test_the_viewer_renames_a_scope_the_moment_its_region_is_learned(tmp_path: Path) -> None:
    """The card is memoized, but not past a name arriving: the scope is in the cache key."""
    store = RegionStore(tmp_path / "regions.json")
    entry = PacketEntry(when=utcnow(), kind="packet", path="", raw=_scoped("harbour"))
    viewer = PacketViewer([entry], 0, resolve=lambda h: "", scope_of=store.scope_of)
    assert "scope harbour" not in _plain(viewer.render_body(80))
    store.learn("harbour", "typed")
    assert "scope harbour" in _plain(viewer.render_body(80))


# -- the live feed -----------------------------------------------------------------------


class _Session:
    def invalidate(self) -> None:
        pass


def _feed(*raws: dict) -> LiveFeedScreen:
    """A live feed over one packet row per frame, oldest first, knowing ``harbour``."""
    screen = LiveFeedScreen(
        session=_Session(),
        resolve=lambda h: "",
        seed=[
            Observation(node="", kind="packet", snr=5.0, rssi=-90.0, observed_at=utcnow(), raw=raw)
            for raw in raws
        ],
        scope_of=_knows("harbour"),
    )
    screen.note_viewport(40)
    return screen


def test_the_feed_carries_a_scope_lane_on_a_72_column_terminal() -> None:
    """The framed body there is 68 cells: the icon-only row holds the lane, name and all.

    The lane sits between the subject and the readings; a plain flood is a muted dash and
    a direct frame, which has no scope, leaves the lane blank.
    """
    screen = _feed(_DIRECT, _FLOOD, _scoped("elsewhere"), _scoped("harbour"))
    header, *rows = _stripped(screen.render_body(68))
    lanes = header.split()
    assert lanes.index("SCOPE") == lanes.index("SNR") - 1
    for line in (header, *rows):
        assert cell_len(line.rstrip()) <= 68
    col = _col(header, "SCOPE")
    assert _col(rows[0], "harbour") == col
    assert _col(rows[1], f"? {_code('elsewhere')}") == col
    assert _col(rows[2], " - ") + 1 == col
    assert "-" not in rows[3].replace("-9", "")  # the direct frame's lane is blank
    assert all(row.rstrip().endswith("dBm") for row in rows)


def test_the_feed_keeps_the_class_label_ahead_of_the_scope() -> None:
    """Between the two widths that hold it, the label wins: a class is never ``unscoped``."""
    screen = _feed(_scoped("harbour"))
    header = _stripped(screen.render_body(72))[0]
    assert "CLASS" in header and "SCOPE" not in header
    header, row = _stripped(screen.render_body(100))[:2]
    assert "CLASS" in header and "SCOPE" in header
    assert _col(header, "SCOPE") == _col(row, "harbour")


def test_the_feed_leaves_the_scope_to_the_viewer_on_the_picocalc() -> None:
    """At 53 the row already scrolls sideways; a lane only ←→ reaches is not drawn."""
    screen = _feed(_scoped("harbour"))
    body = _plain(screen.render_body(53))
    assert "SCOPE" not in body and "harbour" not in body


def test_the_feed_hands_its_scope_reader_to_the_viewer() -> None:
    """Enter opens a card that names the same region the row did."""
    screen = _feed(_scoped("harbour"))
    opened: list = []
    screen._session.run_screen = lambda viewer: opened.append(viewer)  # noqa: SLF001
    screen._session.run_detached = lambda coro: None  # noqa: SLF001
    screen.handle("enter")
    assert "flood · scope harbour" in _plain(opened[0].render_body(80))


# -- message paths -----------------------------------------------------------------------


def test_a_messages_scope_is_read_once_off_its_arrivals() -> None:
    """Every copy carries the sender's code, so the first flooded copy answers.

    A named reading beats an unnamed one, any scope beats unscoped, and direct copies
    have nothing to say.
    """
    scope_of = _knows("harbour")
    when = utcnow()

    def arrival(raw):  # noqa: ANN001, ANN202
        return Arrival(when=when, hops=(), snr=None, frame=raw)

    direct, flood = arrival(_DIRECT), arrival(_FLOOD)
    named, unnamed = arrival(_scoped("harbour")), arrival(_scoped("elsewhere"))
    assert message_scope([direct, unnamed, named], scope_of) == Scope(
        "scoped", "harbour", _code("harbour")
    )
    assert message_scope([flood, unnamed], scope_of).state == "unknown"
    assert message_scope([direct, flood], scope_of) is UNSCOPED
    assert message_scope([direct], scope_of) is None
    assert message_scope([arrival(None)], scope_of) is None


def test_a_stored_scoped_message_resolves_through_its_arrivals(tmp_path: Path) -> None:
    """History keeps what resolution needs, the matcher keeps the frame, collapse keeps it."""
    repo = Repository(tmp_path / "test.db")
    run = repo.start_run("monitor", {}, None)
    now = utcnow()
    wire = "Alice: hi harbour"
    for i, path in enumerate(("3d63", "3d63", "3d63,a1b2")):
        raw = {**_grp_txt_raw(SECRET, wire), **_scoped("harbour")}
        repo.record_observation(
            run,
            Observation(
                node=None,
                kind="packet",
                path=path,
                snr=2.0,
                observed_at=now - timedelta(seconds=5 - i),
                raw=raw,
            ),
        )
    message = ChatMessage(text=wire, is_channel=True, created_at=now)
    arrivals = collapse(channel_arrivals(repo, message, channel_name="#general", secret=SECRET))
    assert len(arrivals) == 2
    store = RegionStore(tmp_path / "regions.json")
    assert message_scope(arrivals, store.scope_of).state == "unknown"
    store.learn("harbour", "channel")
    assert message_scope(arrivals, store.scope_of).region == "harbour"
    repo.close()


def _paths_screen(scope: Scope | None) -> MessagePathsScreen:
    message = ChatMessage(text="hi", is_channel=True, created_at=utcnow())
    return MessagePathsScreen(
        message,
        [Arrival(when=utcnow(), hops=("3d",), snr=1.0)],
        matched=True,
        resolve=lambda h: h,
        prefix_bytes=1,
        self_name="Homestead",
        summary="heard once",
        source="Alice",
        scope=scope,
    )


def test_the_paths_dialog_states_the_scope_once_in_its_title() -> None:
    """A status atom on the title, in the viewer's words — and none for a direct message."""
    assert _paths_screen(Scope("scoped", "harbour", "3fa1")).title == (
        "Message paths · scope harbour"
    )
    assert (
        _paths_screen(Scope("unknown", None, "3fa1")).title == "Message paths · unknown scope 3fa1"
    )
    assert _paths_screen(UNSCOPED).title == "Message paths · unscoped"
    assert _paths_screen(None).title == "Message paths"
    # …and never per arrival row: the body does not repeat it.
    body = _plain(_paths_screen(Scope("scoped", "harbour", "3fa1")).render_body(72))
    assert "harbour" not in body


# -- monitor -------------------------------------------------------------------------------


def _capture(renderer_cls, rows: list[dict]) -> str:  # noqa: ANN001
    buffer = io.StringIO()
    with renderer_cls(script.console(buffer)).stream(_live_packets()) as emit:
        for row in rows:
            emit(row)
    return buffer.getvalue()


def _record(scope: Scope | None, name: str) -> dict:
    from meshterm.ui.fields import NodeRef

    return {
        "observed_at": utcnow(),
        "node": NodeRef(name=name, hash="d4e5f6a7"),
        "kind": "packet",
        "snr_db": 1.0,
        "rssi_dbm": -90.0,
        "position": None,
        "scope": scope,
        "path": [],
    }


def test_monitor_streams_the_scope_before_the_name() -> None:
    """A pinned SCOPE lane on the plain face, and NAME still last.

    A long region overruns its lane and pushes the row right, so the one field built to be
    pushed is the only one it can push.
    """
    text = _capture(
        PlainRenderer,
        [
            _record(Scope("scoped", "harbour", "3fa1"), "Alice"),
            _record(Scope("unknown", None, "3fa1"), "Bob"),
            _record(UNSCOPED, "Carol"),
            _record(None, "Dave"),
        ],
    )
    header, *rows = text.splitlines()
    assert header.split()[-2:] == ["SCOPE", "NAME"]
    assert [row.split()[-2:] for row in rows] == [
        ["harbour", "Alice"],
        ["unknown", "Bob"],
        ["unscoped", "Carol"],
        ["-", "Dave"],
    ]
    assert (
        len(
            {
                row.index(name)
                for row, name in zip(rows, "Alice Bob Carol Dave".split(), strict=True)
            }
        )
        == 1
    )


def test_monitor_json_carries_the_scope_object_or_null() -> None:
    """The document speaks the shared scope shape; a frame with none is ``null``."""
    text = _capture(
        JsonRenderer, [_record(Scope("scoped", "harbour", "3fa1"), "Alice"), _record(None, "Bob")]
    )
    first, second = (json.loads(line) for line in text.splitlines())
    assert first["scope"] == {"state": "scoped", "region": "harbour", "code": "3fa1"}
    assert second["scope"] is None
    assert list(first)[-2:] == ["scope", "path"]


# -- the channel list --------------------------------------------------------------------


def _channel_header(scopes: dict[int, str]) -> str:
    """The Channels list's column header, with the given slots scoped."""
    from meshterm.core.channel_probe import ChannelSlot
    from meshterm.ui.channels import _LiveStats, _menu_items
    from meshterm.ui.tui.select import SelectScreen
    from tests.test_gallery import DEFAULT_PUBLIC_SECRET, _ChannelsCtx

    slots = [
        ChannelSlot(idx=0, name="Public", secret=DEFAULT_PUBLIC_SECRET),
        ChannelSlot(idx=1, name="Ops", secret=bytes(range(16))),
    ]
    ctx = _ChannelsCtx(muted=set(), scopes={slots[i].identity: n for i, n in scopes.items()})
    title, items = _menu_items(ctx, slots, 8, _LiveStats(ctx))
    return _stripped(SelectScreen(title, items).render_body(68))[0]


def test_the_channel_list_draws_scope_only_when_a_channel_has_one() -> None:
    """A column of blanks says nothing; one scoped channel brings the lane back."""
    assert _channel_header({}).split() == ["CHANNEL", "UNREAD", "LAST", "MSGS", "ACTIVITY"]
    assert _channel_header({1: "harbour"}).split() == [
        "CHANNEL",
        "SCOPE",
        "UNREAD",
        "LAST",
        "MSGS",
        "ACTIVITY",
    ]
