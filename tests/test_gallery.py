# SPDX-License-Identifier: Apache-2.0
"""The dual-platform gallery.

Each screen that the interactive menu can open, checked for the width on both platforms
that the app runs on.

Each entry below builds one full-screen :class:`~meshterm.ui.tui.screen.Screen` with data
that the test builds by hand (in the form of the simulator). This is the "fake session,
real screen" method that the test file of each individual screen already uses. Then the
test renders the screen two ways: directly (``render_body``), and through the real frame
compositor (``compose_base``). It does this at 72x24 for REGULAR. It does this at the two
live row counts of PICOCALC_LYRA (53x26, which is the real floor on the handheld, and 53x40,
which is the case of the boot font and the 6x8 font B). It also does this at 53x14 for
CARDPUTER_ZERO. No rendered line can be wider than its terminal.

This is the harness for platform parity, and the work on the PicoCalc was built around it.
If a screen is added here and does not work on both platforms, CI fails by default. Thus
the suite checks new work automatically, and nobody must remember to check by eye. The
suite already covers each screen that the interactive menu can open from the start. It does
not start empty. If a screen does not yet fit 53 columns, the suite marks it ``xfail`` in
:data:`_KNOWN_WIDE`, and does not leave it out. Thus this list is the worklist of P6 for the
reduction of width, and P6 works through it screen by screen. A screen leaves the list when
its case reports XPASS.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.text import Text

from meshterm.core.channels import DEFAULT_PUBLIC_SECRET, derive_secret
from meshterm.core.courier_store import CourierStore
from meshterm.core.models import (
    NODE_TYPE_CHAT,
    NODE_TYPE_REPEATER,
    NODE_TYPE_SENSOR,
    ChatMessage,
    Contact,
    Conversation,
    Hop,
    Observation,
    TraceResult,
    TraceStats,
    TxLevelResult,
    utcnow,
)
from meshterm.core.preferences import Preferences
from meshterm.core.regions import frame_scope, region_key, scope_body, transport_code
from meshterm.core.remote_store import CachedValue
from meshterm.core.watch_store import WatchStore
from meshterm.persistence.repository import DiscoveredPath
from meshterm.platforms import (
    CARDPUTER_ZERO,
    PICOCALC_LYRA,
    REGULAR,
    Platform,
    get_platform,
    set_platform,
)
from meshterm.services.courier import CourierService
from meshterm.services.message_paths import Arrival
from meshterm.services.monitor_service import ACTIVITY_BUCKETS
from meshterm.services.records import CATEGORY_BY_ID
from meshterm.services.topology import MeshTopology, build_topology
from meshterm.ui.about import (
    AboutPage,
    about_author,
    about_meshterm,
    join_discord,
    support_project,
)
from meshterm.ui.attribution import CREDIT_FULL, CREDIT_SHORT
from meshterm.ui.chat import ChatScreen
from meshterm.ui.config_editor import _ConfigMenu, _menu_items, config_table, has_pin
from meshterm.ui.contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING
from meshterm.ui.contacts_screen import ContactsScreen
from meshterm.ui.courier_screen import CourierOutboxScreen
from meshterm.ui.dashboard_screen import DashboardScreen
from meshterm.ui.device_info_screen import DeviceInfoScreen
from meshterm.ui.fontset import FONTS
from meshterm.ui.livefeed_screen import LiveFeedScreen
from meshterm.ui.map_render import MapMarker
from meshterm.ui.map_screen import MapScreen
from meshterm.ui.message_paths_screen import MessagePathsScreen
from meshterm.ui.node_detail_screen import NodeDetailScreen, _Action, _RoutesView, _Tab
from meshterm.ui.packet_viewer import PacketEntry, PacketViewer
from meshterm.ui.path_composer import PathComposerScreen
from meshterm.ui.preferences import _menu_items as _preference_items
from meshterm.ui.records_screen import RecordScreen, WalkVertex
from meshterm.ui.remote_cli import RemoteCliScreen
from meshterm.ui.repeater_admin import AdminMenu
from meshterm.ui.repeater_admin import _menu_items as _admin_menu_items
from meshterm.ui.theme import name_style
from meshterm.ui.timemachine_screen import TimeMachineScreen
from meshterm.ui.trace_screen import TraceScreen
from meshterm.ui.tui import Screen, SelectScreen, fkeys, frame
from meshterm.ui.tui.prompt import CountdownDialog, ReconnectDialog
from meshterm.ui.tx_screen import TxSweepScreen
from meshterm.ui.walk_screen import WalkScreen
from meshterm.ui.widgets import (
    NODE_GLYPHS,
    ContactsSort,
    highlighted_hash,
    node_marker,
    self_marker,
)
from tests.conftest import plain as _plain

_HUB_KEY = "3d63c6429436" + "0" * 52
_FAR_KEY = "f2c24f54551e" + "0" * 52


class _GallerySession:
    """A minimal stand-in for TuiSession that each gallery screen accepts.

    It has the same shape as the class that the test file of each screen makes for itself,
    as ``_FakeSession`` or ``_StubSession``.
    """

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.repaints = 0
        self.stack: list = []
        self._cols, self._rows = cols, rows

    def invalidate(self) -> None:
        self.repaints += 1

    def push(self, screen) -> None:  # noqa: ANN001
        self.stack.append(screen)

    def pop(self, screen=None) -> None:  # noqa: ANN001
        if screen is None:
            self.stack.pop()
        elif screen in self.stack:
            self.stack.remove(screen)

    def base_body_size(self) -> tuple[int, int]:
        return self._cols, self._rows

    async def button_dialog(self, prompt, buttons, **kwargs):  # noqa: ANN001
        return None


@dataclass
class _Entry:
    """One gallery specimen: a name, and the method to build it at a given terminal size."""

    name: str
    factory: Callable[[int, int], Screen]


# --- factories: one for each screen that the interactive menu can open -----------------


def _dashboard(cols: int, rows: int) -> Screen:
    return DashboardScreen(
        session=_GallerySession(cols, rows),
        resolve=lambda h: {"a1b2c3d4": "Alice", "3d63c642": "Hilltop-Repeater"}.get(h, ""),
        window=[
            Observation(
                node="a1b2c3d4",
                name="a1b2c3d4",
                kind="advert",
                snr=5.0,
                rssi=-90.0,
                observed_at=utcnow(),
            ),
            Observation(
                node="3d63c642",
                name="3d63c642",
                kind="packet",
                snr=-2.0,
                rssi=-104.0,
                observed_at=utcnow(),
            ),
        ],
        activity=lambda: (2.0,) * ACTIVITY_BUCKETS,
        activity_flags=lambda: (True,) * ACTIVITY_BUCKETS,
        kind_counts=lambda: {"packet:ADVERT": 5, "packet:ACK": 2, "packet": 3},
    )


def _dashboard_scoped(cols: int, rows: int) -> Screen:
    """The dashboard for one region: its title, its F3 chip, and its sections for the scope.

    The name of the region is long on purpose, so the title needs its short form on the
    console.
    """
    from meshterm.core.regions import Scope

    def scope_of(raw):  # noqa: ANN001, ANN202 - the shape of RegionStore.scope_of
        return Scope("scoped", "laurentides-nord", "beef") if raw else None

    frame = {"payload_typename": "GRP_TXT", "route_typename": "TC_FLOOD"}
    screen = DashboardScreen(
        session=_GallerySession(cols, rows),
        resolve=lambda h: "",
        window=[
            Observation(
                node=None, kind="packet", snr=3.5, rssi=-98.0, observed_at=utcnow(), raw=dict(frame)
            )
        ],
        activity=lambda: (2.0,) * ACTIVITY_BUCKETS,
        activity_flags=lambda: (True,) * ACTIVITY_BUCKETS,
        kind_counts=lambda: {"packet:GRP_TXT": 5},
        scope_of=scope_of,
    )
    screen.handle("scope")
    return screen


def _contacts(cols: int, rows: int) -> Screen:
    sort = ContactsSort.from_name("name", SORT_COLUMNS, SORT_OPENS_ASCENDING)
    contacts = [
        Contact(name="Alice", public_key="aa" * 32),
        Contact(name="A Rather Long Repeater Name For Width", public_key=_HUB_KEY),
    ]
    # The archived count is not zero, so the end of the list draws both maintenance rows.
    # This is its populated state, and the widest that the end of the list gets. There is also
    # a locked contact, so that its padlock must follow the glyph contract of both platforms.
    return ContactsScreen(
        "Homestead",
        "cc" * 32,
        contacts,
        1,
        {"aa" * 6: 7},
        sort,
        archived=12,
        locked=frozenset({"aa" * 32}),
    )


def _archive_ranked():  # noqa: ANN201
    """A ranked contact table for the screens of the sweep. The real function scores it.

    The percentiles are not written by hand, on purpose. The purpose of the gallery is that
    a specimen comes from the own funnels of the app. A percentile is the one number on
    these screens that cannot be written by hand and still be true. It is the rank of a
    contact against the other contacts in the same list. If the test makes it up, the
    screen is not consistent in itself: three contacts show 2, 7, and 11 out of a field of
    three.
    """
    from meshterm.core.contact_score import ContactSignals, rank_contacts

    specs = [
        (
            "Alice",
            "aa" * 32,
            dict(
                heard_age_days=0.2,
                packets=140,
                dm_total=18,
                dm_age_days=2.0,
                known_days=300.0,
                hops=0.0,
            ),
        ),
        (
            "A Rather Long Repeater Name For Width",
            _HUB_KEY,
            dict(heard_age_days=95.0, packets=3, known_days=200.0, hops=2.0),
        ),
        ("hop-9", "9a" * 32, dict(heard_age_days=400.0, packets=1, known_days=420.0, hops=4.0)),
        ("Lakeside", "3d" * 32, dict(heard_age_days=30.0, packets=22, known_days=250.0, hops=1.0)),
        ("sensor-2", "7c" * 32, dict(heard_age_days=210.0, packets=2, known_days=260.0)),
    ]
    # The types are as the names say. Thus the preview draws the ``▲`` of a repeater and the
    # ``◉`` of a sensor, next to the plain ``●`` of ``hop-9``, which never advertised a type.
    types = {
        "Alice": NODE_TYPE_CHAT,
        "A Rather Long Repeater Name For Width": NODE_TYPE_REPEATER,
        "Lakeside": NODE_TYPE_REPEATER,
        "sensor-2": NODE_TYPE_SENSOR,
    }
    contacts = [
        Contact(name=n, public_key=k, key_prefix=k[:12], node_type=types.get(n))
        for n, k, _ in specs
    ]
    signals = {k[:12]: ContactSignals(node=k[:12], **sig) for _, k, sig in specs}
    return rank_contacts(contacts, signals)


def _archive_victims():  # noqa: ANN201
    """The contacts that the sweep removes: the weakest half of the ranking, weakest first."""
    from meshterm.core.contact_score import sweep_candidates

    ranked = _archive_ranked()
    return sweep_candidates(ranked, keep=2)


def _archive_ladder(cols: int, rows: int) -> Screen:
    from meshterm.ui.sweep_screen import _target_screen

    ranked = _archive_ranked()
    return _target_screen(ranked, [r for r in ranked if not r.protected])


def _archive_preview(cols: int, rows: int) -> Screen:
    from meshterm.ui.sweep_screen import _preview_screen

    return _preview_screen(_archive_victims())


def _archived(cols: int, rows: int) -> Screen:
    from meshterm.core.contact_store import RememberedContact
    from meshterm.ui.archived_screen import ArchivedScreen, archived_rows
    from meshterm.ui.contactlist import (
        ARCHIVED_SORT_COLUMNS,
        ARCHIVED_SORT_OPENS_ASCENDING,
    )

    now = int(datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc).timestamp())
    remembered = [
        RememberedContact(public_key="9a" * 32, name="hop-9", archived_at=now - 3 * 86400),
        RememberedContact(
            public_key=_HUB_KEY,
            name="A Rather Long Repeater Name For Width",
            node_type=2,
            archived_at=now - 40 * 86400,
        ),
        RememberedContact(public_key="7c" * 32, name="sensor-2", archived_at=None),
    ]
    return ArchivedScreen(
        archived_rows(remembered),
        1,
        ContactsSort.from_name("archived", ARCHIVED_SORT_COLUMNS, ARCHIVED_SORT_OPENS_ASCENDING),
    )


def _node_detail_mark() -> Text:
    # The code uses the own marker and name styles of the app, not a hex colour that is
    # written by hand. The gallery is a specimen of what the platform draws, so a colour in a
    # stub can hide a bug in the palette.
    glyph, glyph_style = NODE_GLYPHS[NODE_TYPE_REPEATER]
    mark = Text(f"{glyph} ", style=glyph_style)
    mark.append("Hilltop-Repeater", style=name_style("Hilltop-Repeater", _HUB_KEY))
    return mark


def _node_detail_header() -> Text:
    header = _node_detail_mark()
    header.append("   repeater", style="muted")
    return header


def _node_detail(cols: int, rows: int, *, with_minimap: bool = False) -> Screen:
    minimap = None
    if with_minimap:
        from meshterm.ui.minimap import MiniMap

        minimap = MiniMap(
            _GallerySession(cols, rows),
            _StubTileSource(),
            14,
            center_lat=45.40,
            center_lon=-73.50,
            zoom=12,
            markers=[MapMarker("Hilltop-Repeater", 45.40, -73.50, is_repeater=True)],
        )
    return NodeDetailScreen(
        title="Node — Hilltop-Repeater",
        header=_node_detail_header(),
        info_rows=[
            ("key", highlighted_hash(_HUB_KEY, 1)),
            ("heard", Text("5m ago")),
            ("packets", Text("42")),
        ],
        tabs=[_Tab("Info", "info"), _Tab("Routes", "routes")],
        minimap=minimap,
        map_caption=None,
        routes=_RoutesView(note="no route observed yet — trace to discover one"),
        info_actions=[
            _Action("timemachine", "⏳", "", "Time machine — 42 receptions"),
            # The destructive row of the page. It is here so that the specimen shows how a
            # delete looks on each platform: a red 🗑 on the desktop, and a tint on the
            # words where the PicoCalc has no icon lane.
            _Action("remove", "🗑", "err", "Remove contact…"),
        ],
        trace_action=_Action("trace", "\U0001f3af", "", "Trace — auto route …"),
        mark=_node_detail_mark(),
    )


class _StubTileSource:
    """A tile source that is always offline: the gallery renders only markers, with no network."""

    available = False
    max_zoom = 14

    def load_tile(self, z: int, x: int, y: int):  # noqa: ANN001
        return None

    def answered_empty(self, z: int, x: int, y: int) -> bool:  # noqa: ANN001
        return False  # offline means no answer. It is never the source that says "nothing there"

    def resident(self, z: int, x: int, y: int):  # noqa: ANN001
        return None  # RAM never holds anything

    def warm(self, z: int, x: int, y: int) -> None:  # noqa: ANN001
        return None


def _map_body_rows(rows: int) -> int:
    """The body height that the map gets in the frame into which these specimens compose.

    The map is the only screen that sets the size of its own canvas from
    :meth:`~meshterm.ui.tui.session.TuiSession.base_body_size`. Thus the stub session must
    answer with the height of the body, not the height of the terminal. If it does not, the
    specimen draws a canvas that is taller than the slice in which it is shown. This cuts
    the bottom row (and, on the borderless platform, the basemap credit that is in that
    row). It also makes a "↓ more" that is not real, for a map that never scrolls. This
    function does the same as ``base_body_size`` with no header, because these specimens
    compose against an empty header. The result is the footer row, and also the two borders
    of the panel or the one title bar of the borderless platform.
    """
    return max(1, rows - (3 if get_platform().frame_border else 2))


def _map(cols: int, rows: int) -> Screen:
    """The map when the user arrives: no key is pressed yet, so the whole basemap credit shows."""
    session = _GallerySession(cols, _map_body_rows(rows))
    markers = [
        MapMarker("Homestead", 45.50, -73.60, is_self=True),
        MapMarker("Hilltop-Repeater", 45.40, -73.50, is_repeater=True),
        MapMarker("A Rather Long Node Name For Width", 45.55, -73.65),
    ]
    return MapScreen(session, markers, _StubTileSource(), 14)


def _map_panned(cols: int, rows: int) -> Screen:
    """The map after use: the credit is now its short form (refer to ui/attribution.py)."""
    screen = _map(cols, rows)
    screen.render_body(cols)  # the paint at arrival, which the user answers
    screen.handle("right")
    return screen


def _map_find(cols: int, rows: int) -> Screen:
    """A map in use, with a live find. On the PicoCalc the query and the credit share a row."""
    screen = _map_panned(cols, rows)
    for ch in "Hilltop":
        screen.handle("text", ch)
    return screen


def _node_detail_map(cols: int, rows: int) -> Screen:
    """The node page with its preview of the location, which also credits the basemap."""
    return _node_detail(cols, rows, with_minimap=True)


def _node_detail_regions(cols: int, rows: int) -> Screen:
    """The node page of a repeater, with its regions answered. The row is longest here."""
    from meshterm.core.region_store import RegionStore
    from meshterm.ui.node_detail_screen import regions_value

    store = RegionStore(Path(tempfile.mkdtemp(prefix="meshterm-gallery-regions-")) / "r.json")
    store.learn_carried(_HUB_KEY[:12], ["*", "lakeside", "lakeside-north", "harbour"])
    screen = _node_detail(cols, rows)
    assert isinstance(screen, NodeDetailScreen)
    screen.replace_info_row("regions", regions_value(store, _HUB_KEY[:12], routed=True))
    screen.replace_info_actions(
        [
            _Action("timemachine", "⏳", "", "Time machine — 42 receptions"),
            _Action("regions", "🔖", "", "Ask which regions it carries"),
            _Action("remove", "🗑", "err", "Remove contact…"),
        ]
    )
    return screen


def _region_editor(cols: int, rows: int) -> Screen:
    """The region editor when it has the most content.

    The dump is cut, the rest is recovered, and the edits are not saved.
    """
    from meshterm.core.region_admin import parse_region_dump
    from meshterm.core.region_sim import SimulatedRegionMap
    from meshterm.ui.region_editor import RegionMenu, region_items

    hub = Contact(name="Hilltop-Repeater", public_key=_HUB_KEY, key_prefix="3d63c6429436")
    # A table that is too big for one reply, with the answer that a repeater gives.
    sim = SimulatedRegionMap(
        [
            ("lakeside", "*", True),
            ("lakeside-north", "lakeside", True),
            ("lakeside-south", "lakeside", False),
            ("harbour", "*", True),
            ("harbour-east", "harbour", True),
            ("harbour-west", "harbour", True),
            ("old-town", "*", False),
            ("old-town-market", "old-town", True),
            ("riverside", "*", True),
            ("riverside-upper", "riverside", True),
            ("a-region-named-thirty-bytes-xx", "riverside", True),
        ]
    )
    sim.command("region default harbour")
    table = (
        parse_region_dump(sim.command("region"))
        .with_default(sim.command("region default"))
        .with_lists(sim.command("region list allowed"), sim.command("region list denied"))
    )
    title, items = region_items(hub, table, 2)
    return RegionMenu(title, items)


def _chat(cols: int, rows: int) -> Screen:
    conv = Conversation(
        label="Alice",
        is_channel=False,
        contact=Contact(name="Alice", public_key="d4" + "0" * 62, key_prefix="d4e5f6a7"),
    )
    messages = [
        ChatMessage(text="on my way, should be there soon", outbound=False, peer="d4e5f6a7"),
        ChatMessage(
            text="sounds good, see you shortly", outbound=True, peer="d4e5f6a7", acked=True
        ),
    ]
    return ChatScreen(
        conv,
        messages,
        send=None,
        names={"d4e5f6a7": "Alice"},
        session=_GallerySession(cols, rows),
    )


def _room(cols: int, rows: int, *, access=None, retry: bool = False) -> Screen:  # noqa: ANN001
    """The board of a room when it is widest: each type of author, and the access atom of the title.

    The board has a post by a contact (with a name, in its hue), a post by an author that
    nothing names (the grey hash), a notice that the room posted itself, and our own post.
    If ``retry`` is set, our post has no ack, so ^R joins ^L on the hint. This is the longest
    that the hint gets.
    """
    from meshterm.core.models import LoginResult, RoomAccess, RoomLogin
    from meshterm.ui.room import RoomScreen

    room = Contact(name="Lakeside BBS", public_key="f6" * 32, key_prefix="f6" * 6, node_type=3)
    peer = room.key_prefix
    messages = [
        ChatMessage(text="Anyone driving to the swap meet Saturday?", peer=peer, author="d4e5f6a7"),
        ChatMessage(
            text="Is the north repeater down? Nothing since 6.", peer=peer, author="e5f6a7b8"
        ),
        ChatMessage(
            text="Reminder: this board keeps the last 32 posts.", peer=peer, author="f6f6f6f6"
        ),
        ChatMessage(text="I'll check it tonight", outbound=True, peer=peer, acked=not retry),
    ]
    names = {"d4e5f6a7": "Alice", "f6f6f6f6": room.name}

    async def never(*_):  # noqa: ANN002, ANN202 - nobody presses a key in the gallery
        return RoomLogin(LoginResult.NO_REPLY)

    return RoomScreen(
        Conversation(label=room.name, is_channel=False, contact=room),
        messages,
        send=never,
        names={},
        session=_GallerySession(cols, rows),
        resolve=lambda h: names.get(h or "", h),
        auto_login=never,
        join=never,
        access=access or RoomAccess.MEMBER,
        resend=never,
    )


class _RoomsCtx:
    """The minimal AppContext surface that the Rooms page and the page of a room read."""

    def __init__(self) -> None:
        from meshterm.core.models import RoomAccess

        self._access = {"Lakeside BBS": RoomAccess.MEMBER}
        self.rooms = SimpleNamespace(
            joined=lambda room: room.name in self._access,
            access=lambda room: self._access.get(room.name),
        )
        self.chat = SimpleNamespace(unread=lambda key: 128 if key.endswith("f6" * 6) else 0)
        lasts = {
            f"dm:{'f6' * 6}": ChatMessage(
                text="x", peer="f6" * 6, author="d4e5f6a7", created_at=utcnow()
            )
        }
        self.repo = SimpleNamespace(
            last_chat_messages=lambda rooms=(): lasts,
            recent_chat_messages=lambda **kw: [lasts[f"dm:{'f6' * 6}"]],
        )


def _gallery_rooms() -> list[Contact]:
    """A joined room with a long name and a busy board, and two rooms heard but not joined."""
    from datetime import timedelta

    now = utcnow()
    return [
        Contact(
            name="Lakeside BBS and Swap Meet",
            public_key="f6" * 32,
            key_prefix="f6" * 6,
            node_type=3,
            last_seen=now,
            route_hops=("a1", "b2", "c3", "d4", "e5"),
        ),
        Contact(
            name="Hilltop Swap",
            public_key="b7" * 32,
            key_prefix="b7" * 6,
            node_type=3,
            last_seen=now - timedelta(days=6),
        ),
        Contact(name="Old Room", public_key="c8" * 32, key_prefix="c8" * 6, node_type=3),
    ]


def _rooms_page(cols: int, rows: int) -> Screen:
    """The Rooms page when it is widest.

    It has a long name, a badge of three digits, and a room that was never heard.
    """
    from meshterm.ui.rooms import _PAGE_HINT, _page_items

    ctx = _RoomsCtx()
    ctx._access = {"Lakeside BBS and Swap Meet": ctx._access["Lakeside BBS"]}
    title, items = _page_items(ctx, _gallery_rooms())
    return SelectScreen(title, items, footer_hint=_PAGE_HINT)


def _room_login_card(cols: int, rows: int) -> Screen:
    """The busy card of the join when it is widest.

    It has a long room name, a long route, and a long wait.
    """
    from meshterm.ui.tui.screen import BusyDialog

    return BusyDialog(
        "logging in to YJN-ROOM-OBS St-Jean\nalong its 6-hop route… 137 s", title="Rooms"
    )


def _room_page(cols: int, rows: int) -> Screen:
    """The page of a joined room: its summary is over its actions.

    The badge is on Open the board.
    """
    from meshterm.ui.rooms import _detail_items, _summary

    ctx = _RoomsCtx()
    room = _gallery_rooms()[0]
    ctx._access = {room.name: ctx._access["Lakeside BBS"]}
    return SelectScreen(
        f"Room — {room.name}",
        _detail_items(ctx, room),
        prompt=_summary(ctx, room),
        footer_hint="↑↓ move · Enter select · Esc back",
    )


def _room_read_only(cols: int, rows: int) -> Screen:
    """A member with read-only access: the compose line gives way to a line that says why."""
    from meshterm.core.models import RoomAccess

    return _room(cols, rows, access=RoomAccess.READ_ONLY)


def _room_retry(cols: int, rows: int) -> Screen:
    """A post of ours with no ack: ^R retry joins ^L on the hint."""
    return _room(cols, rows, retry=True)


def _chat_channel_scoped(cols: int, rows: int) -> Screen:
    """A channel with a send scope: the ``· scope`` atom of the title, and a resend under it.

    This is the widest real case. The title has a region name at the maximum of the
    firmware, 30 bytes. One message went out with this scope. One message is unscoped and
    has the muted tail. The hint ^R and the *Resend* chip of the lane are both live on the
    newest message, which has a scope.
    """
    region = "lakeside-north-shore-emergency"  # 30 bytes, the longest that the firmware allows
    conv = Conversation(label="Lakeside emergency", is_channel=True, channel_idx=2)
    messages = [
        ChatMessage(text="Alice: anyone on the north shore?", is_channel=True),
        ChatMessage(
            text="here — relaying for the south side", outbound=True, is_channel=True, scope="*"
        ),
        ChatMessage(
            text="road closed past the marina", outbound=True, is_channel=True, scope=region
        ),
    ]

    async def resend(message: ChatMessage) -> ChatMessage:  # pragma: no cover - not pressed
        return message

    return ChatScreen(
        conv,
        messages,
        send=None,
        names={},
        session=_GallerySession(cols, rows),
        scope=region,
        resend_unscoped=resend,
    )


def _chat_urls(cols: int, rows: int) -> Screen:
    """A channel message with two links: on the PicoCalc, two codes are side by side under it.

    This is the longest pair that still fits side by side in the 44 cells after the indent
    of the body. Thus the width gate sees the widest band that a transcript draws. Regular
    draws no codes.
    """
    conv = Conversation(label="Lakeside emergency", is_channel=True, channel_idx=2)
    messages = [
        ChatMessage(
            text=(
                "Alice: shelters on https://meshterm.net/map and the road list at "
                "https://github.com/jpmartineau/MeshTerm"
            ),
            is_channel=True,
        ),
        ChatMessage(text="thanks, on it", outbound=True, is_channel=True),
    ]
    return ChatScreen(conv, messages, send=None, names={}, session=_GallerySession(cols, rows))


def _chat_long_url(cols: int, rows: int) -> Screen:
    """A picked message with a link that is wider than the body lane.

    The link goes out of the lane whole, and the hint names ^U. The gate of 72 cells on
    Regular sees the link start at the gutter and the hint with ``^U QR``. On the PicoCalc,
    the same link folds under the indent (it is wider than the whole panel) and its code is
    below it, and the lane lights ``QR``.
    """
    conv = Conversation(label="Lakeside emergency", is_channel=True, channel_idx=2)
    messages = [
        ChatMessage(
            text=(
                "Alice: the full road list is at "
                "https://github.com/jpmartineau/MeshTerm/blob/main/meshterm/ui/chat.py "
                "if anyone needs it"
            ),
            is_channel=True,
        ),
    ]
    screen = ChatScreen(conv, messages, send=None, names={}, session=_GallerySession(cols, rows))
    screen.handle("up")
    return screen


class _PickerChat:
    """The unread counter that the rows of the picker read live."""

    def __init__(self, unread: dict[str, int]) -> None:
        self._unread = unread

    def unread(self, key: str) -> int:
        return self._unread.get(key, 0)


class _PickerDevstate:
    """The device reads, cached in the session, from which the picker builds itself."""

    def __init__(self, contacts: list[Contact], slots: list) -> None:
        self._contacts = contacts
        self._slots = slots

    async def channel_slots(self) -> list:
        return list(self._slots)

    async def contacts(self) -> list[Contact]:
        return list(self._contacts)


class _PickerRepo:
    """The stored history for the age, the badge, and the preview of the last message in a row."""

    def __init__(self, lasts: dict) -> None:
        self._lasts = lasts

    def last_chat_messages(self, rooms=()) -> dict:  # noqa: ANN001 - the shape of the repository
        return dict(self._lasts)

    def node_names(self) -> dict:
        return {}


def _chat_picker(cols: int, rows: int) -> Screen:
    """The conversation picker, built through the own row funnel of the tool.

    This is its widest case. It has a name at the maximum of the lane, an unread badge that
    can have three digits, and a last message that is much longer than any terminal. The
    row scrolls with ←→, so MeshTerm must cut the message and not fit it (refer to
    :meth:`meshterm.tools.chat.ChatTool._picker_items`).
    """
    from meshterm.tools.chat import ChatTool

    contacts = [
        Contact(name="Alice", public_key="aa" * 32, key_prefix="aa" * 6, node_type=1),
        Contact(name="A Rather Long Contact Name", public_key="d4" * 32, key_prefix="d4" * 6),
        Contact(name="Homestead", public_key="60" * 32, key_prefix="60" * 6, node_type=1),
        # A room whose latest post is by an author that nothing names (the hash is used
        # instead), and a room that was never joined, whose preview lane says so.
        Contact(name="Lakeside BBS", public_key="f6" * 32, key_prefix="f6" * 6, node_type=3),
        Contact(name="Hilltop Swap", public_key="b7" * 32, key_prefix="b7" * 6, node_type=3),
    ]
    now = datetime(2026, 7, 12, 14, 30, tzinfo=timezone.utc)
    lasts = {
        # The default public channel, with the name that _channels_from_slots gives it when
        # no slots are read.
        "chan:slot:0": ChatMessage(
            text="Alice: anyone up around the Plateau tonight? testing a new antenna",
            is_channel=True,
            channel_idx=0,
            channel_id="slot:0",
            created_at=now,
        ),
        f"dm:{'aa' * 6}": ChatMessage(
            text="on my way, should be there soon - the bridge is backed up again",
            peer="aa" * 6,
            created_at=now,
        ),
        f"dm:{'f6' * 6}": ChatMessage(
            text="Is the north repeater down? Nothing since 6, and the solar was fine",
            peer="f6" * 6,
            author="e5f6a7b8",
            created_at=now,
        ),
    }
    ctx = _PickerCtx(contacts, lasts)
    items = asyncio.run(ChatTool()._picker_items(ctx))
    return SelectScreen(
        "Chat - pick a conversation",
        items,
        footer_hint="↑↓ move · type to filter · Enter open · Esc back",
        delete_hint="Del erase",
    )


def _channels_manager(cols: int, rows: int) -> Screen:
    """The channel manager when it is widest: each lane has data, and each action row is drawn.

    The test builds it through the own row funnel of the feature. Thus the code that the app
    runs lays out the header line, the glyph lane, the unread badge, the counts, the ages,
    and the sparkline. The cases that matter are here. There is a name at the maximum of the
    lane. There is a send scope at the maximum of its lane (cut with an ellipsis), next to a
    short scope and channels with no scope. There is a muted channel (its mark folds to a
    different glyph on the console), a channel with no messages at all, and an unread badge
    with three digits.
    """
    from meshterm.core.channel_probe import ChannelSlot
    from meshterm.ui.channels import _MANAGER_HINT, _LiveStats, _menu_items

    slots = [
        ChannelSlot(idx=0, name="Public", secret=DEFAULT_PUBLIC_SECRET),
        ChannelSlot(idx=1, name="Lakeside emergency", secret=bytes(range(16))),
        ChannelSlot(idx=2, name="#montreal", secret=derive_secret("#montreal")),
        ChannelSlot(idx=3, name="Ops", secret=bytes(range(16, 32))),
    ]
    scopes = {
        slots[1].identity: "lakeside-north-shore-emergency",
        slots[2].identity: "harbour",
    }
    ctx = _ChannelsCtx(muted={slots[3].identity}, scopes=scopes)
    title, items = _menu_items(ctx, slots, 8, _LiveStats(ctx))
    return SelectScreen(title, items, footer_hint=_MANAGER_HINT)


def _channel_detail(cols: int, rows: int, *, scope: str | None = None) -> Screen:
    """The action page of one channel, with its summary line above the rows."""
    from meshterm.core.channel_probe import ChannelSlot
    from meshterm.ui.channels import _detail_items, _detail_summary, _LiveStats

    slot = ChannelSlot(idx=2, name="Lakeside emergency", secret=bytes(range(16)))
    ctx = _ChannelsCtx(muted=set(), scopes={slot.identity: scope} if scope else None)
    stats = _LiveStats(ctx)
    return SelectScreen(
        f"Channel — {slot.name}",
        _detail_items(ctx, slot),
        prompt=_detail_summary(ctx, slot, stats),
        footer_hint="↑↓ move · Enter select · Esc back",
    )


def _channel_detail_scoped(cols: int, rows: int) -> Screen:
    """The same page with a send scope of 30 bytes, the maximum.

    This shows the atom and the row of the summary.
    """
    return _channel_detail(cols, rows, scope="lakeside-north-shore-emergency")


def _scope_picker(cols: int, rows: int) -> Screen:
    """The value picker for the send scope.

    The user can choose no scope, a known region with its carriers, or a typed region.
    """
    from meshterm.ui.channels import _SCOPE_HINT, _scope_items

    ctx = _ChannelsCtx(
        muted=set(),
        regions={"lakeside": 3, "lakeside-north-shore-emergency": 1, "harbour": 0},
    )
    return SelectScreen(
        "Send scope — Lakeside emergency",
        _scope_items(ctx, "lakeside"),
        prompt="The region this channel's messages flood into:",
        default="lakeside",
        footer_hint=_SCOPE_HINT,
    )


class _ChannelsChat:
    """The surface of the chat service that the channel rows read: unread counts and mute state."""

    def __init__(self, muted: set[str]) -> None:
        self._muted = muted

    def unread(self, key: str) -> int:
        """A badge with three digits on one channel, so that the lane is at its widest."""
        return 128 if key.endswith("slot:1") or "Lakeside" in key else 0


class _ChannelsCtx:
    """The minimal AppContext surface that the channel rows and the detail page read."""

    def __init__(
        self,
        muted: set[str],
        *,
        scopes: dict[str, str] | None = None,
        regions: dict[str, int] | None = None,
    ) -> None:
        self.chat = _ChannelsChat(muted)
        self.repo = _ChannelsRepo()
        self.preferences = Preferences()
        self._muted = muted
        scopes = scopes or {}
        regions = regions or {}
        # The read surface of the region store: the scope of each channel, and the known
        # regions with the number of repeaters that were heard to carry each region.
        self.region_store = SimpleNamespace(
            channel_scope=lambda identity: scopes.get(identity),
            names=lambda: list(regions),
            carriers=lambda name: ("3d63c6429436",) * regions.get(name, 0),
        )

    @property
    def mute_store(self):  # noqa: ANN201 - a stand-in for the real store
        """A store that answers only the one question that a row asks it."""
        return SimpleNamespace(is_muted=lambda identity: identity in self._muted)


class _ChannelsRepo:
    """Channel statistics with one busy channel, one quiet channel, and one that was never used."""

    def channel_stats(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        """Return nothing. The rows then draw their empty marks, which is the narrower case."""
        return {}


class _PickerCtx:
    """The minimal AppContext surface that :meth:`ChatTool._picker_items` reads."""

    def __init__(self, contacts: list[Contact], lasts: dict) -> None:
        self.devstate = _PickerDevstate(contacts, [])
        self.repo = _PickerRepo(lasts)
        self.chat = _PickerChat({"chan:slot:0": 3, f"dm:{'aa' * 6}": 12, f"dm:{'f6' * 6}": 4})
        self.rooms = SimpleNamespace(joined=lambda room: room.name == "Lakeside BBS")


def _livefeed(cols: int, rows: int) -> Screen:
    return LiveFeedScreen(
        session=_GallerySession(cols, rows),
        resolve=lambda h: {"a1b2c3d4": "Alice"}.get(h, ""),
        seed=[
            Observation(
                node="a1b2c3d4",
                name="a1b2c3d4",
                kind="advert",
                snr=5.0,
                rssi=-90.0,
                observed_at=utcnow(),
            ),
        ],
    )


#: The region against which the scope specimens resolve, and a channel-text payload to scope.
_REGION = "harbour"
_SCOPED_PAYLOAD = bytes.fromhex("a71c2d00112233445566778899aabbccddeeff")


def _scoped_frame(region: str) -> dict:
    """The raw payload of an RX-log packet for a channel text that is flooded under ``region``."""
    code = transport_code(region_key(region), scope_body(5, _SCOPED_PAYLOAD))
    return {
        "route_typename": "TC_FLOOD",
        "payload_type": 5,
        "payload_typename": "GRP_TXT",
        "chan_hash": "a7",
        "pkt_payload": _SCOPED_PAYLOAD,
        "transport_code": code.to_bytes(2, "little").hex() + "0000",
    }


def _scope_of(raw: dict | None):  # noqa: ANN202 - the answer of the region store, as a stand-in
    """Resolve the scope of a packet against the one region that the specimens know by name."""
    return frame_scope(raw, (_REGION,)) if isinstance(raw, dict) else None


def _livefeed_scopes(cols: int, rows: int) -> Screen:
    """The feed with four packets.

    They are a scoped packet, a packet with an unknown scope, an unscoped packet, and a
    direct packet.
    """
    frames = [
        _scoped_frame(_REGION),
        _scoped_frame("elsewhere"),  # a region that nobody here has named
        {"route_typename": "FLOOD", "payload_typename": "GRP_TXT", "chan_hash": "a7"},
        {"route_typename": "DIRECT", "payload_typename": "TEXT_MSG", "dest_hash": "a1"},
    ]
    return LiveFeedScreen(
        session=_GallerySession(cols, rows),
        resolve=lambda h: {"a1": "Alice"}.get(h, ""),
        seed=[
            Observation(node="", kind="packet", snr=5.0, rssi=-90.0, observed_at=utcnow(), raw=raw)
            for raw in frames
        ],
        scope_of=_scope_of,
    )


def _walk_topo() -> tuple[MeshTopology, dict[str, Contact]]:
    hub = Contact(name="Hilltop-Repeater", public_key=_HUB_KEY, key_prefix="3d63c6429436")
    far = Contact(name="Alice", public_key=_FAR_KEY, key_prefix="f2c24f54551e")
    topo = MeshTopology("aa" * 6, contacts=[hub, far])
    hub_id, far_id = topo.canonical(hub.public_key), topo.canonical(far.public_key)
    when = utcnow()
    topo.add_walk([topo.self_id, hub_id], snrs=[6.0], when=when, source="trace")
    topo.add_walk([hub_id, far_id], snrs=[-2.0], when=when, source="packet")
    return topo, {hub_id: hub, far_id: far}


def _timemachine_scoped(cols: int, rows: int) -> Screen:
    """The whole-mesh page for one scope: its title, its F2 chip, and its charts.

    The name of the region is long on purpose, so the title needs its short form on the
    console.
    """
    from datetime import timedelta

    from meshterm.ui.timemachine_screen import _mesh_scope_sections

    now = utcnow()
    stamps = [now - timedelta(hours=h) for h in (150, 90, 60, 30, 20, 5, 2)]

    def build(window, width, scope):  # noqa: ANN001, ANN202
        return _mesh_scope_sections(stamps, window, width) if scope else [Text("page")]

    screen = TimeMachineScreen(
        session=_GallerySession(cols, rows),
        label="the whole mesh",
        build=build,
        scopes=lambda _window: {("unscoped",), ("region", "laurentides-nord")},
    )
    screen.handle("scope")
    screen.handle("scope")
    return screen


def _walk(cols: int, rows: int) -> Screen:
    topo, contacts = _walk_topo()
    return WalkScreen(
        session=_GallerySession(cols, rows),
        topo=topo,
        contacts=contacts,
        self_label="Homestead",
    )


def _timemachine(cols: int, rows: int) -> Screen:
    def build(window, width, scope):  # noqa: ANN001
        return [Text("Hilltop-Repeater  5m ago  advert"), Text("Alice  12m ago  packet")]

    return TimeMachineScreen(
        session=_GallerySession(cols, rows),
        label="Hilltop-Repeater",
        build=build,
    )


def _message_paths(cols: int, rows: int) -> Screen:
    message = ChatMessage(
        text="on my way, should be there soon", is_channel=True, created_at=utcnow()
    )
    arrivals = [
        Arrival(when=utcnow(), hops=("3d63c6",), snr=4.0),
        Arrival(when=utcnow(), hops=("a1b2c3", "77aabb"), snr=-2.0),
    ]
    return MessagePathsScreen(
        message,
        arrivals,
        matched=True,
        resolve=lambda h: h,
        prefix_bytes=1,
        self_name="Homestead",
        summary="heard twice",
        source="Alice",
    )


def _message_paths_scoped(cols: int, rows: int) -> Screen:
    """A message that is flooded under a known region: its scope is in the title, one time."""
    screen = _message_paths(cols, rows)
    return MessagePathsScreen(
        screen._message,
        screen._arrivals,
        matched=True,
        resolve=lambda h: h,
        prefix_bytes=1,
        self_name="Homestead",
        summary="heard twice",
        source="Alice",
        scope=_scope_of(_scoped_frame(_REGION)),
    )


def _message_paths_unknown_scope(cols: int, rows: int) -> Screen:
    """A message flooded under a region that nobody here has named: the code is used instead."""
    screen = _message_paths(cols, rows)
    return MessagePathsScreen(
        screen._message,
        screen._arrivals,
        matched=True,
        resolve=lambda h: h,
        prefix_bytes=1,
        self_name="Homestead",
        summary="heard twice",
        source="Alice",
        scope=_scope_of(_scoped_frame("elsewhere")),
    )


def _message_paths_sent_scope(cols: int, rows: int) -> Screen:
    """Our send with a scope that no node relayed.

    The line says that no known repeater carries the scope.
    """
    from meshterm.ui.chat import _sent_scope_line

    message = ChatMessage(
        text="road closed past the marina",
        outbound=True,
        is_channel=True,
        created_at=utcnow(),
        scope="lakeside-north-shore-emergency",
    )
    ctx = SimpleNamespace(region_store=SimpleNamespace(carriers=lambda name: ()))
    return MessagePathsScreen(
        message,
        [],
        matched=True,
        resolve=lambda h: h,
        prefix_bytes=1,
        self_name="Homestead",
        summary="no copies in the packet log",
        source="Homestead",
        sent_scope=_sent_scope_line(ctx, message, relayed=False),
    )


def _remote_cli(cols: int, rows: int) -> Screen:
    return RemoteCliScreen(
        node_label="Hilltop-Repeater",
        history=["get name", "get name -> Hilltop-Repeater"],
        send=lambda c: None,
        session=_GallerySession(cols, rows),
    )


def _path_composer(cols: int, rows: int) -> Screen:
    hub = Contact(name="Hilltop-Repeater", public_key=_HUB_KEY, key_prefix="3d63c6429436")
    far = Contact(name="Alice", public_key=_FAR_KEY, key_prefix="f2c24f54551e")
    topo = build_topology(
        self_id="aaaaaaaaaaaa" + "0" * 52,
        contacts=[hub, far],
        trace_paths=[],
        packet_paths=[],
        neighbour_links=[],
    )
    return PathComposerScreen(
        device_label="Homestead",
        device_hash="aaaaaaaaaaaa" + "0" * 52,
        topology=topo,
        width_bytes=1,
        hops=["3d63c6429436", "f2c24f54551e"],
    )


class _CourierStubDevice:
    async def get_contacts(self) -> list:
        return []


class _CourierStubChat:
    def __init__(self) -> None:
        self.sent: list = []

    async def send_direct(self, contact, text):  # noqa: ANN001
        return None


class _CourierStubContext:
    """The minimal AppContext surface that :class:`CourierOutboxScreen` reads."""

    def __init__(self, config_dir: Path) -> None:
        self.courier_store = CourierStore(config_dir / "courier.json")
        self.watch_store = WatchStore(config_dir / "watchtower.json")
        self.chat = _CourierStubChat()
        self.is_connected = True
        self.log = logging.getLogger("test.gallery.courier")
        self._device = _CourierStubDevice()

    async def device(self) -> _CourierStubDevice:
        return self._device


def _courier_outbox(cols: int, rows: int) -> Screen:
    config_dir = Path(tempfile.mkdtemp(prefix="meshterm-gallery-courier-"))
    ctx = _CourierStubContext(config_dir)
    ctx.courier = CourierService(ctx)
    ctx.courier_store.queue("aa" * 6, "Hilltop-Repeater", "battery reading requested please")
    ctx.courier_store.queue("bb" * 6, "A Rather Long Contact Name For Width", "hello there")
    return CourierOutboxScreen(ctx)


def _record(**over) -> DiscoveredPath:
    defaults = dict(
        id=1,
        category="grand_tour",
        width_bytes=1,
        spec="3d63c6429436,f2c24f54551e",
        route=("3d63c6429436", "f2c24f54551e"),
        score=2.0,
        stats={
            "hop_count": 2,
            "distinct_nodes": 2,
            "repeats": False,
            "min_snr": 6.0,
            "km_travelled": 3.2,
            "km_complete": True,
            "far_km": 1.5,
            "rtt_ms": 250.0,
        },
        app_version="0.1.0",
        discovered_at=datetime(2026, 7, 12, 14, 30, tzinfo=timezone.utc),
    )
    defaults.update(over)
    return DiscoveredPath(**defaults)


def _record_dialog(cols: int, rows: int) -> Screen:
    record = _record()
    return RecordScreen(
        record,
        CATEGORY_BY_ID[record.category],
        1,
        resolve=lambda h: {"3d63c6429436": "Hilltop-Repeater", "f2c24f54551e": "Alice"}.get(h, h),
        device_label="Homestead",
        device_hash=None,
    )


def _record_area(cols: int, rows: int) -> Screen:
    """A record with positioned hops, on its Area tab: the drawing has the size of the stage."""
    record = _record()
    sglyph, scolor = self_marker()
    hglyph, hcolor = node_marker(NODE_TYPE_REPEATER)
    nglyph, ncolor = node_marker(1)
    screen = RecordScreen(
        record,
        CATEGORY_BY_ID[record.category],
        1,
        resolve=lambda h: {"3d63c6429436": "Hilltop-Repeater", "f2c24f54551e": "Alice"}.get(h, h),
        device_label="Homestead",
        device_hash=None,
        far_label="Alice",
        shape=[
            WalkVertex(0.0, 0.0, sglyph, scolor, True),
            WalkVertex(4.0, 1.5, hglyph, hcolor, False, label="3d"),
            WalkVertex(2.5, 3.0, nglyph, ncolor, False, label="f2"),
        ],
    )
    screen.note_metrics(rows, rows)
    screen.handle("shift_tab")  # Info → Area, the last tab
    return screen


def _record_route(cols: int, rows: int) -> Screen:
    """A record on its Route tab: the graph, its legend, the route line, and the trace row."""
    screen = _record_dialog(cols, rows)
    screen.handle("tab")
    return screen


def _packet_viewer(cols: int, rows: int) -> Screen:
    entry = PacketEntry(
        when=utcnow(),
        kind="packet",
        node="3d63c6429436",
        path="3d63c6429436f2c24f54551e",
        raw={"payload_typename": "GRP_TXT", "route_typename": "FLOOD"},
    )
    return PacketViewer(
        [entry],
        0,
        resolve=lambda h: {"3d63c6429436": "Hilltop-Repeater"}.get(h, h),
    )


def _packet_viewer_scoped(cols: int, rows: int, region: str = _REGION) -> Screen:
    """A channel text that is flooded under a region: the route row names the region."""
    entry = PacketEntry(
        when=utcnow(),
        kind="packet",
        path="3d63c6429436f2c24f54551e",
        raw=_scoped_frame(region),
    )
    return PacketViewer(
        [entry],
        0,
        resolve=lambda h: {"3d63c6429436": "Hilltop-Repeater"}.get(h, h),
        scope_of=_scope_of,
    )


def _packet_viewer_unknown_scope(cols: int, rows: int) -> Screen:
    """A channel text flooded under a region that nobody here named: its code is used instead."""
    return _packet_viewer_scoped(cols, rows, region="elsewhere")


def _trace(cols: int, rows: int) -> Screen:
    async def _noop_flow(current):  # noqa: ANN001
        return current

    async def _trace_call(path_spec, on_trace):  # noqa: ANN001
        pass  # never called: the test gives data to the screen directly with _on_trace below

    screen = TraceScreen(
        "Alice",
        mode="target",
        device_label="Homestead",
        device_hash="aa" * 32,
        resolve=lambda label: label,
        session=_GallerySession(cols, rows),
        trace=_trace_call,
        compose_path=_noop_flow,
        explore=_noop_flow,
        pick_width=_noop_flow,
        pick_samples=_noop_flow,
        width_bytes=lambda: 2,
        sample_count=lambda: 1,
        pace_s=0.0,
        previous=None,
        auto_spec=lambda: "",
        auto_source="",
    )
    screen._on_trace(
        TraceResult(
            target="Alice",
            success=True,
            hops=[Hop(0, "3d63c6", 5.0), Hop(1, None, 2.0)],
            round_trip_ms=210.0,
            path_hash_bytes=2,
        )
    )
    return screen


def _tx_sweep(cols: int, rows: int) -> Screen:
    screen = TxSweepScreen(
        admin_label="Hilltop-Repeater",
        target_label="Alice",
        device_label="Homestead",
        device_hash="00" * 32,
        resolve=lambda h: h,
        session=_GallerySession(cols, rows),
        tx_min=12,
        tx_max=28,
        step=3,
        samples=3,
        run_sweep=lambda: None,
        apply_winner=lambda: None,
    )
    screen.on_phase("coarse")
    screen.on_level(
        1,
        6,
        TxLevelResult(
            tx_power=19,
            samples=3,
            successes=3,
            target_snr=8.8,
            score=8.8,
            stats=TraceStats.from_traces("Alice", []),
        ),
    )
    return screen


#: A device snapshot in the shape of a companion, for the Device info page. It has each
#: field that the config table draws, with the pairing PIN that the table conceals.
_DEVICE_SNAPSHOT = {
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


def _device_info(cols: int, rows: int) -> Screen:
    return DeviceInfoScreen(
        _GallerySession(cols, rows),
        lambda reveal: config_table(_DEVICE_SNAPSHOT, {}, reveal_pin=reveal),
        title="Device info",
        conceals=has_pin(_DEVICE_SNAPSHOT),
    )


def _device_info_revealed(cols: int, rows: int) -> Screen:
    # The same page after ^S: the widest state of the row that the other case masks.
    screen = _device_info(cols, rows)
    screen.handle("reveal")
    return screen


def _config_editor(cols: int, rows: int) -> Screen:
    """The Device config editor: each setting is staged from one grouped list."""
    return _ConfigMenu(
        _GallerySession(cols, rows),
        lambda reveal: _menu_items(_DEVICE_SNAPSHOT, {"tx_power": 14}, 1, reveal),
        conceals=has_pin(_DEVICE_SNAPSHOT),
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
    )


def _config_editor_revealed(cols: int, rows: int) -> Screen:
    # The same list after ^S, where the PIN row is at its widest.
    screen = _config_editor(cols, rows)
    screen.handle("reveal")
    return screen


def _repeater_admin(cols: int, rows: int) -> Screen:
    """The repeater admin page when it is widest: values read, one value staged, and Apply drawn."""
    hub = Contact(name="Hilltop-Repeater", public_key=_HUB_KEY, key_prefix="3d63c6429436")
    now = utcnow()
    cache = {
        "name": CachedValue("Hilltop-Repeater", now),
        "txdelay": CachedValue("0.5", now),
        "bridge.delay": CachedValue("", now, supported=False),
    }
    title, items = _admin_menu_items(hub, cache, {"txdelay": "1.5"})
    return AdminMenu(title, items, footer_hint="↑↓ move · type to filter · Enter select · Esc back")


def _preferences(cols: int, rows: int) -> Screen:
    """The Preferences page when it is widest.

    It has one saved override, one staged value, and the reset row.
    """
    prefs = Preferences()
    prefs.set("trace_cooldown_s", 2.5)
    title, items = _preference_items(prefs, {"history_days": 90})
    return SelectScreen(
        title,
        items,
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
    )


def _preferences_weekly_advert(cols: int, rows: int) -> Screen:
    """The Preferences page with the weekly advert on, and its longest status line drawn."""
    from meshterm.services.advert_scheduler import QUIET, AdvertStatus

    prefs = Preferences()
    prefs.set("weekly_flood_advert", True)
    prefs.set("advert_quiet_s", 60)
    title, items = _preference_items(prefs, {}, lambda: AdvertStatus(QUIET))
    return SelectScreen(
        title,
        items,
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
    )


def _cooldown_countdown(cols: int, rows: int) -> Screen:
    """The countdown of the transmit cooldown when it is widest.

    It shows the wait of a flood advert and its reason.

    This is a dialog and not a menu screen. It is here for the one check that only this
    harness does. The box has the size of its own content, so a longer reason line would go
    over the 53 columns of the PicoCalc, and nothing else would find it.
    """
    return CountdownDialog("Flood advert", 47.0, reason="a flood advert reaches the whole mesh")


def _reconnect_reason(cols: int, rows: int) -> Screen:
    """The reconnect dialog after a refusal that continues: the reason wraps under the spinner.

    The size comes from its own content, as for the countdown. The reason is the longest
    that a reconnect can get on a serial port. It is the reason of Linux, which also names
    ModemManager.
    """
    from meshterm.core.connection import DeviceCommandError
    from meshterm.ui.device_picker import connect_failure_text

    dialog = ReconnectDialog("Still trying to reconnect…")
    dialog.set_detail(
        connect_failure_text(
            DeviceCommandError(
                "/dev/ttyACM0 is in use by another program — the MeshCore app, a flasher, or "
                "a serial monitor. Close it and try again. On Linux this is often "
                "ModemManager, which probes new USB serial devices: `sudo systemctl stop "
                "ModemManager` and try again."
            )
        )
    )
    return dialog


def _about_meshterm(cols: int, rows: int) -> Screen:
    return AboutPage("About MeshTerm", about_meshterm())


def _about_author(cols: int, rows: int) -> Screen:
    return AboutPage("About the author", about_author())


def _support_project(cols: int, rows: int) -> Screen:
    return AboutPage("Support MeshTerm", support_project())


def _join_discord(cols: int, rows: int) -> Screen:
    return AboutPage("Join Discord", join_discord())


def _diagnostics(cols: int, rows: int) -> Screen:
    """The Diagnostics page, with the widest values that a real page can have.

    A deep Windows config directory and a connection error in the own words of the radio
    are the two fields that have no natural limit. Both are here on purpose. A markdown list
    item hangs its wrapped text under itself at any width, and this must work in 53
    columns.
    """
    from meshterm.ui.diagnostics import DiagnosticsPage

    return DiagnosticsPage(
        _diagnostics_source(),
        session=_GallerySession(cols, rows),
        save_path=Path("C:/Users/somebody/.meshterm/meshterm-diagnostics.md"),
    )


def _diagnostics_source() -> str:
    """A typical diagnostics document, built in the way that the tool builds one."""
    from datetime import datetime

    from meshterm.tools.diagnostics import markdown_source
    from meshterm.ui import fields
    from meshterm.ui.report import Column, Facts, Lane, Listing

    report = (
        Facts(
            key="meshterm",
            caption="Build",
            fields=(fields.word("meshterm", "meshterm"), fields.word("install", "install")),
            values={"meshterm": "0.3.7", "install": "frozen"},
        ),
        Facts(
            key="terminal",
            caption="Terminal",
            fields=(
                fields.word("terminal", "terminal"),
                fields.word("terminal_size", "terminal_size"),
                fields.flag("over_ssh", "over_ssh"),
                fields.word("powerline", "powerline"),
            ),
            values={
                "terminal": "Windows Terminal",
                "terminal_size": "53x26",
                "over_ssh": True,
                "powerline": "none (font:windows-terminal)",
            },
        ),
        Facts(
            key="device",
            caption="Radio",
            fields=(
                fields.flag("connected", "connected"),
                fields.path("config_dir", "config_dir"),
                fields.free("error", "error"),
            ),
            values={
                "connected": False,
                "config_dir": Path("C:/Users/somebody/AppData/Roaming/meshterm/profiles/handheld"),
                "error": "no companion answered on COM7 within 10s; the port is open elsewhere",
            },
        ),
        Listing(
            key="tables",
            caption="Stored rows",
            columns=(
                fields.word("table", "TABLE"),
                Column(key="rows", lanes=(Lane(header="ROWS", render=str, align="right"),)),
            ),
            rows=[{"table": "observations", "rows": 418_203}, {"table": "runs", "rows": 91}],
        ),
        Listing(
            key="preferences",
            caption="Changed preferences",
            columns=(fields.word("preference", "PREFERENCE"), fields.free("value", "VALUE")),
            rows=[{"preference": "log_level", "value": "DEBUG"}],
        ),
    )
    return markdown_source(report, when=datetime(2026, 9, 19, 17, 0))


def _quit_hold(cols: int, rows: int) -> Screen:
    """The box that a held Esc key opens, with its bar half full: one second before the quit."""
    from meshterm.services.hold_to_quit import DIALOG_S, QUIT_S
    from meshterm.ui.tui.holdquit import HoldQuitDialog

    return HoldQuitDialog(0.0, clock=lambda: (DIALOG_S + QUIT_S) / 2)


def _share_qr(cols: int, rows: int) -> Screen:
    """The share screen: the code and the link of a contact card on a bare frame (the widest QR)."""
    from meshterm.ui.qr import QrScreen

    url = "meshcore://contact/add?name=Lakeside&public_key=" + "ab" * 32 + "&type=2"
    return QrScreen(url, title="Share Lakeside")


def _links_qr(cols: int, rows: int) -> Screen:
    """The two links of a chat message on the share screen.

    The screen shows one code, with ←→ on each side of its URL.
    """
    from meshterm.ui.qr import QrScreen

    return QrScreen(
        "https://meshterm.net/map", "https://github.com/jpmartineau/MeshTerm", title="Links"
    )


_ENTRIES: list[_Entry] = [
    _Entry("dashboard", _dashboard),
    _Entry("dashboard-scoped", _dashboard_scoped),
    _Entry("contacts", _contacts),
    _Entry("archive_ladder", _archive_ladder),
    _Entry("archive_preview", _archive_preview),
    _Entry("archived", _archived),
    _Entry("node_detail", _node_detail),
    _Entry("node_detail_map", _node_detail_map),
    _Entry("node_detail_regions", _node_detail_regions),
    _Entry("map", _map),
    _Entry("map_panned", _map_panned),
    _Entry("map_find", _map_find),
    _Entry("chat", _chat),
    _Entry("chat_channel_scoped", _chat_channel_scoped),
    _Entry("chat_urls", _chat_urls),
    _Entry("chat_long_url", _chat_long_url),
    _Entry("chat_picker", _chat_picker),
    _Entry("room", _room),
    _Entry("room_read_only", _room_read_only),
    _Entry("room_retry", _room_retry),
    _Entry("rooms_page", _rooms_page),
    _Entry("room_page", _room_page),
    _Entry("room_login_card", _room_login_card),
    _Entry("channels_manager", _channels_manager),
    _Entry("channel_detail", _channel_detail),
    _Entry("channel_detail_scoped", _channel_detail_scoped),
    _Entry("scope_picker", _scope_picker),
    _Entry("livefeed", _livefeed),
    _Entry("livefeed_scopes", _livefeed_scopes),
    _Entry("walk", _walk),
    _Entry("timemachine", _timemachine),
    _Entry("timemachine-scoped", _timemachine_scoped),
    _Entry("message_paths", _message_paths),
    _Entry("message_paths_scoped", _message_paths_scoped),
    _Entry("message_paths_unknown_scope", _message_paths_unknown_scope),
    _Entry("message_paths_sent_scope", _message_paths_sent_scope),
    _Entry("remote_cli", _remote_cli),
    _Entry("path_composer", _path_composer),
    _Entry("courier_outbox", _courier_outbox),
    _Entry("record_dialog", _record_dialog),
    _Entry("record_route", _record_route),
    _Entry("record_area", _record_area),
    _Entry("packet_viewer", _packet_viewer),
    _Entry("packet_viewer_scoped", _packet_viewer_scoped),
    _Entry("packet_viewer_unknown_scope", _packet_viewer_unknown_scope),
    _Entry("trace", _trace),
    _Entry("tx_sweep", _tx_sweep),
    _Entry("device_info", _device_info),
    _Entry("device_info_revealed", _device_info_revealed),
    _Entry("config_editor", _config_editor),
    _Entry("config_editor_revealed", _config_editor_revealed),
    _Entry("repeater_admin", _repeater_admin),
    _Entry("region_editor", _region_editor),
    _Entry("preferences", _preferences),
    _Entry("preferences-weekly-advert", _preferences_weekly_advert),
    _Entry("cooldown_countdown", _cooldown_countdown),
    _Entry("reconnect_reason", _reconnect_reason),
    _Entry("quit_hold", _quit_hold),
    _Entry("about_meshterm", _about_meshterm),
    _Entry("about_author", _about_author),
    _Entry("join_discord", _join_discord),
    _Entry("support_project", _support_project),
    _Entry("share_qr", _share_qr),
    _Entry("links_qr", _links_qr),
    _Entry("diagnostics", _diagnostics),
]

#: The combinations of (platform, cols, rows) under which each entry above renders. The
#: PicoCalc has its live floor (53x26, the measurement on the handheld: refer to the P0
#: appendix of the plan) and the lux case (53x40, the boot fbcon font of today or a future
#: 6x8 font). The arrival of this seam did not change Regular, so it stays the existing
#: standard of 72x24. The Cardputer Zero renders at its one size, 53x14. It has the same
#: width gate as the PicoCalc, so each case is also a hard gate there. The test does not
#: gate rows, because a body scrolls. Thus the screens that are cramped at 14 rows are a
#: worklist that a person judges. They are not a list of xfail cases (refer to the logbook
#: of the port).
_COMBOS: list[tuple[Platform, int, int]] = [
    (REGULAR, REGULAR.readable_cols, REGULAR.readable_rows),
    (PICOCALC_LYRA, PICOCALC_LYRA.readable_cols, PICOCALC_LYRA.readable_rows),
    (PICOCALC_LYRA, PICOCALC_LYRA.readable_cols, 40),
    (CARDPUTER_ZERO, CARDPUTER_ZERO.readable_cols, CARDPUTER_ZERO.readable_rows),
]

#: Entries that are wider than the 53 columns of PICOCALC_LYRA today. (A width overflow does
#: not depend on the row count, so one entry here covers both picocalc-lyra combinations.)
#: P6 emptied this set. The whole P1 worklist left it when the F-key lane replaced the hint
#: strings of each screen, and when the wrapped empty-state note of the path composer no
#: longer put a newline into one row. Thus each picocalc-lyra case is now a hard gate. A
#: new screen that cannot fit 53 columns goes here only with a ticket, and it must not stay.
_KNOWN_WIDE: set[str] = set()

#: Entries whose footer_hint was already wider than the 72-column standard that existed
#: before this platform seam. This phase does not correct them. The rule of 72 columns in
#: CLAUDE.md is older than the seam. The standing guidance is that old chrome that already
#: broke the rule waits for a special pass, and is not changed here as a side task. The set
#: has been empty since the F-lane pass of 2026-08-08. That pass built the hint of the map
#: again around its new view-jump keys, and the hint came back inside the budget.
_PREEXISTING_REGULAR_OVERFLOW: set[str] = set()


#: The specimens by name, for a test that needs one entry and not the whole sweep.
_ENTRY_BY_NAME = {entry.name: entry for entry in _ENTRIES}


def _cases():
    for entry in _ENTRIES:
        for platform, cols, rows in _COMBOS:
            case_id = f"{entry.name}-{platform.name}-{cols}x{rows}"
            marks = []
            if platform.name == "picocalc-lyra" and entry.name in _KNOWN_WIDE:
                marks.append(
                    pytest.mark.xfail(
                        reason=(
                            f"{entry.name} overflows picocalc-lyra's "
                            f"{PICOCALC_LYRA.readable_cols} cols today -- see _KNOWN_WIDE, "
                            "the width-reduction worklist"
                        ),
                        strict=False,
                    )
                )
            if platform is REGULAR and entry.name in _PREEXISTING_REGULAR_OVERFLOW:
                marks.append(
                    pytest.mark.xfail(
                        reason=(
                            f"{entry.name} already overflows the existing 72-col standard, "
                            "pre-dating the platform seam -- not retrofitted here, see "
                            "_PREEXISTING_REGULAR_OVERFLOW"
                        ),
                        strict=False,
                    )
                )
            yield pytest.param(entry, platform, cols, rows, id=case_id, marks=marks)


def _assert_fits(lines: list[str], cols: int, where: str) -> None:
    """Assert that each line of the rendered output is at most ``cols`` cells wide."""
    for i, line in enumerate(lines):
        width = cell_len(_plain(line))
        assert width <= cols, f"{where} line {i} is {width} cells, over {cols} allowed: {line!r}"


@pytest.mark.parametrize("entry,platform,cols,rows", list(_cases()))
def test_gallery_screen_fits_its_platform(
    entry: _Entry,
    platform: Platform,
    cols: int,
    rows: int,
) -> None:
    """Each gallery specimen renders within the width of its platform, raw and with the frame."""
    set_platform(platform)
    screen = entry.factory(cols, rows)
    screen.note_viewport(max(1, rows - 4))  # the same viewport math as compose_base

    _assert_fits(screen.render_body(cols), cols, "render_body")
    # Assert the footer that the platform draws. Regular draws the footer_hint string of each
    # screen. It can have Rich markup (the "[warn]offline[/warn]" of the map), so the test
    # parses it before it measures. The PicoCalc never draws the hint strings. The fixed
    # F-key lane replaces them (Platform.footer_fkeys), so the lane of the screen must fit
    # there.
    if platform.footer_fkeys:
        deck = fkeys.DECKS[platform.lane_deck]
        lane = deck.lane_text(screen.fkey_lane)
        shifted = deck.lane_text(screen.fkey_lane, shifted=True)
        assert cell_len(lane.plain) <= cols, f"F-lane {cell_len(lane.plain)} cells: {lane.plain!r}"
        assert cell_len(shifted.plain) <= cols, (
            f"shifted F-lane {cell_len(shifted.plain)} cells: {shifted.plain!r}"
        )
    else:
        footer_plain = Text.from_markup(screen.footer_hint).plain
        assert cell_len(footer_plain) <= cols, (
            f"footer_hint renders to {cell_len(footer_plain)} cells, over {cols}: {footer_plain!r}"
        )

    composed = frame.compose_base(Text(""), screen, screen.footer_hint, cols, rows)
    _assert_fits(composed.split("\n"), cols, "compose_base")

    # No screen has an exit row. Esc leaves. It is on the keyboards of both platforms, and
    # each footer_hint says so. A row that repeated it used two lines of each screen. On the
    # 26 rows of the PicoCalc, this is one row in thirteen. The one "Back" that stays is the
    # half for the discard of staged changes ("✗ Back — discard …"). It is a choice, not an
    # exit, and it is never the bare word.
    for line in _plain(screen.render_body(cols)).splitlines():
        assert line.strip() != "Back", f"{entry.name}: an exit row came back: {line!r}"

    # P3 assertions, on the rendered ANSI (the contracts of the theme and the fold, not the
    # config). A handheld must not emit anything that is not in its own font
    # (Platform.font): the 512-glyph console font of the PicoCalc, or what the emulator of
    # the Cardputer draws. A console with 16 slots (the PicoCalc) must also not have truecolor
    # or 256-colour SGR. Together these are the parity gate. It finds a stray emoji or hex
    # colour when a screen gets one. Without it, the first sign is a tofu box that someone
    # finds on the handheld.
    if platform.font:
        font = FONTS[platform.font]
        for where, ansi_lines in (
            ("render_body", screen.render_body(cols)),
            ("compose_base", composed.split("\n")),
        ):
            for i, line in enumerate(ansi_lines):
                if not platform.truecolor:
                    assert "[38;2;" not in line and "[48;2;" not in line, (
                        f"{entry.name} {where} line {i} emits truecolor SGR: {line!r}"
                    )
                    assert "[38;5;" not in line and "[48;5;" not in line, (
                        f"{entry.name} {where} line {i} emits 256-colour SGR: {line!r}"
                    )
                strays = {ch for ch in line if ord(ch) >= 0x20 and ord(ch) not in font}
                assert not strays, (
                    f"{entry.name} {where} line {i} has characters outside the "
                    f"{platform.name} font: {sorted(strays)!r} in {line!r}"
                )


#: The map specimens and the credit that each must have. The map that nobody touched has
#: the whole OpenFreeMap line. The two maps that were used have the short form.
_CREDITED: list[tuple[str, bool]] = [
    ("map", True),
    ("map_panned", False),
    ("map_find", False),
]


@pytest.mark.parametrize("name,full", _CREDITED)
@pytest.mark.parametrize("platform,cols,rows", _COMBOS)
def test_gallery_map_shows_the_basemap_credit_where_its_frame_puts_it(
    name: str,
    full: bool,
    platform: Platform,
    cols: int,
    rows: int,
) -> None:
    """Each map specimen credits OpenStreetMap, on the surface that its own frame has.

    The width gate above does not fail for a map that stopped to credit anyone with no
    warning. Thus the specimens also assert the mark itself. On a frame with a border, the
    credit is in the bottom border rule, right-justified, with one rule cell before the
    corner, and the drawing does not change. On the borderless frame, which has no rule, the
    code stamps the credit on the last row of the drawing. Refer to
    :mod:`meshterm.ui.attribution`.
    """
    set_platform(platform)
    screen = _ENTRY_BY_NAME[name].factory(cols, rows)
    screen.note_viewport(max(1, rows - 4))
    expected = CREDIT_FULL if full else CREDIT_SHORT
    body = _plain(screen.render_body(cols))
    composed = frame.compose_base(Text(""), screen, screen.footer_hint, cols, rows)
    lines = [_plain(ln) for ln in composed.split("\n")]

    if platform.frame_border:
        assert "OpenStreetMap" not in body, "the credit reached the drawing on a bordered frame"
        # The corners are rounded, or square where Rich decides that the console is legacy
        # Windows (a pytest run with captured output there). Both are the own bottom rule of
        # the frame.
        rule = next(ln for ln in reversed(lines) if "╰" in ln or "└" in ln)
        assert rule[-1] in "╯┘" and rule[:-1].endswith(f" {expected} ─"), rule
    else:
        assert body.splitlines()[-1].endswith(expected)
    assert any(expected in ln for ln in lines)
