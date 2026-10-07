# SPDX-License-Identifier: Apache-2.0
"""The places where the scope of a flood is shown.

These are the packet viewer, the live feed, message paths, and the monitor.
``test_regions.py`` tests the scope itself: the maths, the store, and the history columns.
These tests are about the screens that show the scope. All these screens follow one rule.
A flood shows the region that it was sent into. Or it shows that it was unscoped, or that
it was scoped to a region that nobody here has named. A direct packet shows no scope.
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

#: A channel-text payload, chosen at random, to scope. The code uses the bytes, not the meaning.
_PAYLOAD = bytes.fromhex("a71c2d00112233445566778899aabbccddeeff")


def _scoped(region: str, payload: bytes = _PAYLOAD, **extra) -> dict:
    """The raw payload of an RX-log packet for a channel text that is flooded in ``region``."""
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
    """The 4-hex code that a :func:`_scoped` packet has."""
    return f"{transport_code(region_key(region), scope_body(5, _PAYLOAD)):04x}"


_FLOOD = {"route_typename": "FLOOD", "payload_typename": "GRP_TXT", "chan_hash": "a7"}
_DIRECT = {"route_typename": "DIRECT", "payload_typename": "TEXT_MSG", "dest_hash": "a1"}


def _knows(*names: str):  # noqa: ANN202 - a scope reader over a fixed set of names
    """A ``scope_of`` that knows exactly ``names``.

    It is a small version of the answer of the region store.
    """
    return lambda raw: frame_scope(raw, names) if isinstance(raw, dict) else None


def _stripped(lines: list[str]) -> list[str]:
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


def _col(line: str, needle: str) -> int:
    """The display column of ``needle``, in cells and not in characters.

    The class icon is two cells.
    """
    return cell_len(line[: line.index(needle)])


# -- the packet viewer -------------------------------------------------------------------


def _card_row(raw: dict, label: str, scope_of=None) -> str | None:  # noqa: ANN001
    """The value of the ``label`` row of the viewer for one packet, as plain text.

    The function returns ``None`` when the row is absent.
    """
    entry = PacketEntry(when=utcnow(), kind="packet", path="3d", raw=raw)
    body = _stripped(
        PacketViewer([entry], 0, resolve=lambda h: "", scope_of=scope_of).render_body(80)
    )
    row = next((line for line in body if line.split(None, 1)[:1] == [label]), None)
    return None if row is None else row.split(None, 1)[1].rstrip()


def _has_scope_row(text: str, value: str) -> bool:
    """Whether a rendered card has a ``scope`` row with the text ``value``."""
    return re.search(rf"^scope\s+{re.escape(value)}\s*$", text, re.M) is not None


def test_the_viewer_names_the_region_a_flood_was_scoped_to() -> None:
    """The viewer names the region that a flood was scoped to.

    ``tc flood`` showed how the packet went, and never where. Now the route row says flood,
    and a separate row says the region.
    """
    assert _card_row(_scoped("harbour"), "route", _knows("harbour")) == "flood"
    assert _card_row(_scoped("harbour"), "scope", _knows("harbour")) == "harbour"


def test_the_viewer_shows_an_unnamed_scopes_code_and_a_plain_flood_as_unscoped() -> None:
    """A scope that no known name gives keeps its code. A plain flood shows unscoped."""
    assert _card_row(_scoped("elsewhere"), "scope", _knows("harbour")) == (
        f"unknown region · code {_code('elsewhere')}"
    )
    assert _card_row(_FLOOD, "scope", _knows("harbour")) == "unscoped"


def test_the_viewer_without_a_store_still_tells_scoped_from_unscoped() -> None:
    """If no names are known, the viewer shows less of the truth, and never a wrong value.

    The code replaces the name with the code.
    """
    assert _card_row(_scoped("harbour"), "scope") == f"unknown region · code {_code('harbour')}"
    assert _card_row(_FLOOD, "scope") == "unscoped"


def test_the_viewer_never_gives_a_direct_frame_a_scope() -> None:
    """The viewer gives no scope to a direct packet.

    A repeater never filters a direct packet by region. The viewer shows the route word
    only, and no scope row.
    """
    assert _card_row(_DIRECT, "route", _knows("harbour")) == "direct"
    assert _card_row(_DIRECT, "scope", _knows("harbour")) is None
    tc_direct = {**_DIRECT, "route_typename": "TC_DIRECT"}
    assert _card_row(tc_direct, "route") == "tc direct"
    assert _card_row(tc_direct, "scope") is None


def test_the_viewer_keeps_the_scope_body_out_of_the_raw_dump(tmp_path: Path) -> None:
    """The viewer keeps the scope body out of the raw dump.

    The restored HMAC input is an internal detail, and the route row already shows it.
    """
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
    assert _has_scope_row(body, "harbour")  # a replayed packet resolves as a live packet does
    assert "scope_body" not in body


def test_the_viewer_renames_a_scope_the_moment_its_region_is_learned(tmp_path: Path) -> None:
    """The viewer renames a scope when it learns its region.

    The card is cached, but the cache does not hold the old card when a name arrives,
    because the scope is part of the cache key.
    """
    store = RegionStore(tmp_path / "regions.json")
    entry = PacketEntry(when=utcnow(), kind="packet", path="", raw=_scoped("harbour"))
    viewer = PacketViewer([entry], 0, resolve=lambda h: "", scope_of=store.scope_of)
    assert not _has_scope_row(_plain(viewer.render_body(80)), "harbour")
    store.learn("harbour", "typed")
    assert _has_scope_row(_plain(viewer.render_body(80)), "harbour")


# -- the live feed -----------------------------------------------------------------------


class _Session:
    def invalidate(self) -> None:
        pass


def _feed(*raws: dict) -> LiveFeedScreen:
    """A live feed with one packet row for each packet, oldest first. The feed knows ``harbour``."""
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


def test_the_feed_scope_lane_sits_between_subject_and_readings() -> None:
    """The scope lane of the feed is between the subject and the readings.

    A plain flood has a muted dash. A direct packet has no scope, so its lane is blank.
    """
    screen = _feed(_DIRECT, _FLOOD, _scoped("elsewhere"), _scoped("harbour"))
    header, *rows = _stripped(screen.render_body(100))
    lanes = header.split()
    assert lanes.index("SUBJECT") + 1 == lanes.index("SCOPE") == lanes.index("SNR") - 1
    col = _col(header, "SCOPE")
    assert _col(rows[0], "harbour") == col
    assert _col(rows[1], f"? {_code('elsewhere')}") == col
    assert _col(rows[2], " - ") + 1 == col
    assert "-" not in rows[3].replace("-9", "")  # the lane of the direct packet is blank
    assert all(row.rstrip().endswith("dBm") for row in rows)


def test_the_feed_never_collapses_a_lane() -> None:
    """The feed never collapses a lane.

    The feed draws the class name and the scope at each width. A narrow row scrolls
    instead. No lane gives way: at 72 columns (a body of 68 cells) the readings go past the
    edge, and the ←→ keys on the highlighted row reach them.
    """
    screen = _feed(_scoped("harbour"))
    for width in (68, 72, 80, 100):
        header, row = _stripped(screen.render_body(width))[:2]
        assert "CLASS" in header and "chan text" in row, width
        assert _col(header, "SCOPE") == _col(row, "harbour"), width
    screen._selected = 0  # noqa: SLF001
    screen.render_body(68)
    assert screen._hmax > 0  # noqa: SLF001 - the row is wider than the body, and ←→ reaches it


def test_the_picocalc_feed_names_the_class_without_an_icon() -> None:
    """The feed on the PicoCalc names the class without an icon.

    On the console, the class icon is a substitute of one glyph. The name alone shows the
    class.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    set_platform(PICOCALC_LYRA)
    try:
        screen = _feed(_scoped("harbour"))
        header, row = _stripped(screen.render_body(53))[:2]
        assert header.split()[:2] == ["TIME", "CLASS"]
        assert _col(header, "CLASS") == _col(row, "chan text")
    finally:
        set_platform(REGULAR)


def test_the_feed_hands_its_scope_reader_to_the_viewer() -> None:
    """The feed gives its scope reader to the viewer.

    Thus Enter opens a card with the same region as the row.
    """
    screen = _feed(_scoped("harbour"))
    opened: list = []
    screen._session.run_screen = lambda viewer: opened.append(viewer)  # noqa: SLF001
    screen._session.run_detached = lambda coro: None  # noqa: SLF001
    screen.handle("enter")
    assert _has_scope_row(_plain(opened[0].render_body(80)), "harbour")


# -- message paths -----------------------------------------------------------------------


def test_a_messages_scope_is_read_once_off_its_arrivals() -> None:
    """The scope of a message is read one time from its arrivals.

    Each copy has the code of the sender, so the first flooded copy gives the answer. A
    reading with a name wins over a reading with no name. Any scope wins over unscoped.
    Direct copies give no scope.
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
    """A stored scoped message resolves through its arrivals.

    The history keeps what the resolution needs. The matcher keeps the packet, and the
    collapse keeps it too.
    """
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
    """The paths dialog states the scope one time, in its title.

    The scope is a status atom on the title, in the words of the viewer. A direct message
    has no atom.
    """
    assert _paths_screen(Scope("scoped", "harbour", "3fa1")).title == (
        "Message paths · scope harbour"
    )
    assert (
        _paths_screen(Scope("unknown", None, "3fa1")).title == "Message paths · unknown scope 3fa1"
    )
    assert _paths_screen(UNSCOPED).title == "Message paths · unscoped"
    assert _paths_screen(None).title == "Message paths"
    # The title does not repeat for each arrival row: the body does not repeat the scope.
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
    """The monitor stream has the scope before the name.

    The plain face has a fixed SCOPE lane, and NAME is still last. A long region goes past
    its lane and pushes the row to the right. Thus the region can push only the one field
    that is made to be pushed.
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
    """The document uses the shared scope shape. A packet with no scope has ``null``."""
    text = _capture(
        JsonRenderer, [_record(Scope("scoped", "harbour", "3fa1"), "Alice"), _record(None, "Bob")]
    )
    first, second = (json.loads(line) for line in text.splitlines())
    assert first["scope"] == {"state": "scoped", "region": "harbour", "code": "3fa1"}
    assert second["scope"] is None
    assert list(first)[-2:] == ["scope", "path"]


# -- the channel list --------------------------------------------------------------------


def _channel_header(scopes: dict[int, str]) -> str:
    """The column header of the Channels list, with a scope for the given slots."""
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
    """The channel list draws the scope lane only when a channel has a scope.

    A column of blanks has no information. One scoped channel brings the lane back.
    """
    assert _channel_header({}).split() == ["SLOT", "CHANNEL", "NEW", "LAST", "MSGS", "ACTIVITY"]
    assert _channel_header({1: "harbour"}).split() == [
        "SLOT",
        "CHANNEL",
        "SCOPE",
        "NEW",
        "LAST",
        "MSGS",
        "ACTIVITY",
    ]


# -- a decoded message's card --------------------------------------------------------------


def test_a_decoded_messages_card_reads_its_scope_off_its_logged_copies(tmp_path: Path) -> None:
    """The card of a decoded message reads its scope from its logged copies.

    A 💬 row has no packet. Its card finds the flooded copy in the log and names the scope.
    """
    from types import SimpleNamespace

    from meshterm.persistence.repository import Repository
    from meshterm.ui.livefeed_screen import message_scope_reader

    text = "Alice: road closed"
    raw = _grp_txt_raw(SECRET, text)
    payload = bytes.fromhex(raw["chan_hash"] + raw["cipher_mac"] + raw["crypted"])
    code = transport_code(region_key("harbour"), scope_body(5, payload))
    raw.update(
        route_typename="TC_FLOOD",
        payload_type=5,
        pkt_payload=payload,
        transport_code=code.to_bytes(2, "little").hex() + "0000",
    )
    repo = Repository(tmp_path / "log.db")
    when = utcnow()
    repo.record_observation(
        repo.start_run("monitor", {}),
        Observation(node=None, kind="packet", path="3d", observed_at=when, raw=raw),
    )
    store = RegionStore(tmp_path / "regions.json")
    ctx = SimpleNamespace(repo=repo, region_store=store)
    read = message_scope_reader(ctx, {1: ("#general", SECRET)})

    entry = PacketEntry(when=when, kind="message", channel=1, text=text)
    assert read(entry).state == "unknown"  # the copy is found, but its region has no name yet
    store.learn("harbour", "typed")
    assert read(entry).region == "harbour"  # the name appears when the region is known

    viewer = PacketViewer([entry], 0, resolve=lambda h: "", message_scope=read)
    assert _has_scope_row(_plain(viewer.render_body(80)), "harbour")
    other = PacketEntry(when=when, kind="message", channel=1, text="Bob: something else")
    assert read(other) is None  # no copy of it in the log, so no scope row and no guess
    assert read(PacketEntry(when=when, kind="message", channel=9, text=text)) is None
    repo.close()
