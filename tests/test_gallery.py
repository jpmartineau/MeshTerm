# SPDX-License-Identifier: Apache-2.0
"""The dual-platform gallery.

Every screen the interactive menu can open, checked for width discipline under both
flavours this app runs as.

Each entry below builds one full-screen :class:`~meshterm.ui.tui.screen.Screen` against
hand-built (simulator-shaped) data — the same "fake session, real screen" approach every
individual screen's own test file already uses — then renders it, both directly
(``render_body``) and through the real frame compositor (``compose_base``), at REGULAR's
72x24 and at PICOCALC's two live/lux row counts (53x26, the actual on-device floor; 53x40,
the boot-font/6x8-font-B case). No rendered line may exceed its terminal's width.

This is the platform-parity harness the PicoCalc work was built around: a
screen added here without surviving both platforms is meant to fail CI by default, so the
suite polices new work automatically instead of relying on someone remembering to check
by eye. It intentionally starts already covering every screen the interactive menu can
open (not growing empty) — a screen that doesn't yet fit 53 columns is marked ``xfail`` in
:data:`_KNOWN_WIDE` instead of being left out, so that list *is* the width-reduction
worklist P6 works through screen by screen; a screen graduates off it the moment its case
starts reporting XPASS.
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
from meshterm.platforms import PICOCALC, REGULAR, Platform, get_platform, set_platform
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
from meshterm.ui.fontset import FONT_CODEPOINTS
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
from meshterm.ui.tui.prompt import CountdownDialog
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
    """A minimal TuiSession stand-in every gallery screen is happy with.

    The same shape each screen's own test file rolls independently as ``_FakeSession``
    or ``_StubSession``.
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
    """One gallery specimen: a name, and how to build it at a given terminal size."""

    name: str
    factory: Callable[[int, int], Screen]


# --- factories: one per screen the interactive menu can open ---------------------------


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
        kind_counts=lambda: {"advert": 5, "ack": 2, "packet": 3},
    )


def _contacts(cols: int, rows: int) -> Screen:
    sort = ContactsSort.from_name("name", SORT_COLUMNS, SORT_OPENS_ASCENDING)
    contacts = [
        Contact(name="Alice", public_key="aa" * 32),
        Contact(name="A Rather Long Repeater Name For Width", public_key=_HUB_KEY),
    ]
    # A non-zero archived tally, so the tail draws both maintenance rows — its populated
    # state, and the widest the tail ever gets — and a locked contact, so its padlock is held
    # to both platforms' glyph contracts.
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
    """A ranked contact table for the sweep's screens — scored by the real function.

    Deliberately *not* hand-stamped percentiles. The whole point of the gallery is that a
    specimen comes out of the app's own funnels, and a percentile is the one number on these
    screens that cannot be written down by hand and still be true: it is a contact's rank
    against the others in the same list, so faking it renders a screen that is internally
    inconsistent — three contacts reading 2, 7 and 11 out of a field of three.
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
    # Typed as their names say, so the preview draws a repeater's ``▲`` and a sensor's ``◉``
    # beside the plain ``●`` of ``hop-9``, which never advertised a type.
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
    """The sweep's victim list: the weakest half of the ranking, weakest first."""
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


def _node_detail_header() -> Text:
    # Through the app's own marker and name styles, not a hand-spelled hex: the gallery is
    # a specimen of what the platform draws, so a stub colour would hide a palette bug.
    glyph, glyph_style = NODE_GLYPHS[NODE_TYPE_REPEATER]
    header = Text(f"{glyph} ", style=glyph_style)
    header.append("Hilltop-Repeater", style=name_style("Hilltop-Repeater", _HUB_KEY))
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
            # The page's destructive row: here so the specimen shows how a delete reads
            # on each platform — a red 🗑 on the desktop, the tint on the words where
            # the PicoCalc drops the icon lane.
            _Action("remove", "🗑", "err", "Remove contact…"),
        ],
        trace_action=_Action("trace", "\U0001f3af", "", "Trace — auto route …"),
    )


class _StubTileSource:
    """An always-offline tile source: the gallery renders markers only, no network."""

    available = False
    max_zoom = 14

    def load_tile(self, z: int, x: int, y: int):  # noqa: ANN001
        return None

    def answered_empty(self, z: int, x: int, y: int) -> bool:  # noqa: ANN001
        return False  # offline is silence, never the source saying "nothing there"


def _map_body_rows(rows: int) -> int:
    """The body height the map would be handed in the frame these specimens compose into.

    The map is the one screen that sizes its own canvas from
    :meth:`~meshterm.ui.tui.session.TuiSession.base_body_size`, so the stub session has to
    answer with the *body* height rather than the terminal's — otherwise the specimen draws
    a canvas taller than the slice it is shown in, which cuts the bottom row (and on the
    borderless platform the basemap credit that rides there) and hangs a phantom "↓ more"
    off a map that never scrolls. Mirrors ``base_body_size`` with no header — these
    specimens compose against an
    empty one — so it is the footer row plus either the panel's two borders or the
    borderless platform's single title bar.
    """
    return max(1, rows - (3 if get_platform().frame_border else 2))


def _map(cols: int, rows: int) -> Screen:
    """The map as it is arrived at: nothing pressed yet, so the whole basemap credit shows."""
    session = _GallerySession(cols, _map_body_rows(rows))
    markers = [
        MapMarker("Homestead", 45.50, -73.60, is_self=True),
        MapMarker("Hilltop-Repeater", 45.40, -73.50, is_repeater=True),
        MapMarker("A Rather Long Node Name For Width", 45.55, -73.65),
    ]
    return MapScreen(session, markers, _StubTileSource(), 14)


def _map_panned(cols: int, rows: int) -> Screen:
    """The map once used: the credit has collapsed to its remnant (see ui/attribution.py)."""
    screen = _map(cols, rows)
    screen.render_body(cols)  # the arrival paint the reader is answering
    screen.handle("right")
    return screen


def _map_find(cols: int, rows: int) -> Screen:
    """A used map with a live find: on the PicoCalc the query and the credit share a row."""
    screen = _map_panned(cols, rows)
    for ch in "Hilltop":
        screen.handle("text", ch)
    return screen


def _node_detail_map(cols: int, rows: int) -> Screen:
    """The node page carrying its location preview, which credits the basemap too."""
    return _node_detail(cols, rows, with_minimap=True)


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


class _PickerChat:
    """The unread counter the picker rows read live."""

    def __init__(self, unread: dict[str, int]) -> None:
        self._unread = unread

    def unread(self, key: str) -> int:
        return self._unread.get(key, 0)


class _PickerDevstate:
    """The session-cached device reads the picker builds itself from."""

    def __init__(self, contacts: list[Contact], slots: list) -> None:
        self._contacts = contacts
        self._slots = slots

    async def channel_slots(self) -> list:
        return list(self._slots)

    async def contacts(self) -> list[Contact]:
        return list(self._contacts)


class _PickerRepo:
    """The stored history behind each row's age, badge and last-message preview."""

    def __init__(self, lasts: dict) -> None:
        self._lasts = lasts

    def last_chat_messages(self) -> dict:
        return dict(self._lasts)

    def node_names(self) -> dict:
        return {}


def _chat_picker(cols: int, rows: int) -> Screen:
    """The conversation picker, built through the tool's own row funnel.

    Its widest case: a name at the lane's ceiling, a three-digit-capable unread badge, and
    a last message far longer than any terminal — which is the row ←→ scroll, so it has to
    be cut rather than fitted (see :meth:`meshterm.tools.chat.ChatTool._picker_items`).
    """
    from meshterm.tools.chat import ChatTool

    contacts = [
        Contact(name="Alice", public_key="aa" * 32, key_prefix="aa" * 6, node_type=1),
        Contact(name="A Rather Long Contact Name", public_key="d4" * 32, key_prefix="d4" * 6),
        Contact(name="Homestead", public_key="60" * 32, key_prefix="60" * 6, node_type=1),
    ]
    now = datetime(2026, 7, 12, 14, 30, tzinfo=timezone.utc)
    lasts = {
        # The default public channel, as _channels_from_slots names it with no slots read.
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
    """The channel manager at its widest: every lane populated, every action row drawn.

    Built through the feature's own row funnel, so the header line, the glyph lane, the
    unread badge, the counts, the ages and the sparkline are laid out by the code the app
    runs. The cases that matter are here: a name at the lane's ceiling, a muted channel (its
    mark folds to a different glyph on the console), a channel with no messages at all, and
    a three-digit unread badge.
    """
    from meshterm.core.channel_probe import ChannelSlot
    from meshterm.ui.channels import _MANAGER_HINT, _LiveStats, _menu_items

    slots = [
        ChannelSlot(idx=0, name="Public", secret=DEFAULT_PUBLIC_SECRET),
        ChannelSlot(idx=1, name="Lakeside emergency", secret=bytes(range(16))),
        ChannelSlot(idx=2, name="#montreal", secret=derive_secret("#montreal")),
        ChannelSlot(idx=3, name="Ops", secret=bytes(range(16, 32))),
    ]
    ctx = _ChannelsCtx(muted={slots[3].identity})
    title, items = _menu_items(ctx, slots, 8, _LiveStats(ctx))
    return SelectScreen(title, items, footer_hint=_MANAGER_HINT)


def _channel_detail(cols: int, rows: int) -> Screen:
    """One channel's action page, its vital-signs line above the rows."""
    from meshterm.core.channel_probe import ChannelSlot
    from meshterm.ui.channels import _detail_items, _detail_summary, _LiveStats

    slot = ChannelSlot(idx=2, name="Lakeside emergency", secret=bytes(range(16)))
    ctx = _ChannelsCtx(muted=set())
    stats = _LiveStats(ctx)
    return SelectScreen(
        f"Channel — {slot.name}",
        _detail_items(ctx, slot),
        prompt=_detail_summary(ctx, slot, stats),
        footer_hint="↑↓ move · Enter select · Esc back",
    )


class _ChannelsChat:
    """The chat service surface the channel rows read: unread counts and mute state."""

    def __init__(self, muted: set[str]) -> None:
        self._muted = muted

    def unread(self, key: str) -> int:
        """A three-digit badge on one channel, so the lane is drawn at its widest."""
        return 128 if key.endswith("slot:1") or "Lakeside" in key else 0


class _ChannelsCtx:
    """The minimal AppContext surface the channel rows and detail page read."""

    def __init__(self, muted: set[str]) -> None:
        self.chat = _ChannelsChat(muted)
        self.repo = _ChannelsRepo()
        self.preferences = Preferences()
        self._muted = muted

    @property
    def mute_store(self):  # noqa: ANN201 - a stand-in for the real store
        """A store answering only the one question a row asks it."""
        return SimpleNamespace(is_muted=lambda identity: identity in self._muted)


class _ChannelsRepo:
    """Channel statistics with one busy channel, one quiet one, and one never used."""

    def channel_stats(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        """Return nothing: the rows then draw their empty marks, which is the tighter case."""
        return {}


class _PickerCtx:
    """The minimal AppContext surface :meth:`ChatTool._picker_items` reads."""

    def __init__(self, contacts: list[Contact], lasts: dict) -> None:
        self.devstate = _PickerDevstate(contacts, [])
        self.repo = _PickerRepo(lasts)
        self.chat = _PickerChat({"chan:slot:0": 3, f"dm:{'aa' * 6}": 12})


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


#: The region the scope specimens resolve against, and a channel-text payload to scope.
_REGION = "harbour"
_SCOPED_PAYLOAD = bytes.fromhex("a71c2d00112233445566778899aabbccddeeff")


def _scoped_frame(region: str) -> dict:
    """An RX-log frame's raw payload for a channel text flooded under ``region``."""
    code = transport_code(region_key(region), scope_body(5, _SCOPED_PAYLOAD))
    return {
        "route_typename": "TC_FLOOD",
        "payload_type": 5,
        "payload_typename": "GRP_TXT",
        "chan_hash": "a7",
        "pkt_payload": _SCOPED_PAYLOAD,
        "transport_code": code.to_bytes(2, "little").hex() + "0000",
    }


def _scope_of(raw: dict | None):  # noqa: ANN202 - the region store's answer, standing in
    """Resolve a frame's scope against the one region the specimens know by name."""
    return frame_scope(raw, (_REGION,)) if isinstance(raw, dict) else None


def _livefeed_scopes(cols: int, rows: int) -> Screen:
    """The feed carrying a scoped, an unknown-scoped, an unscoped and a direct frame."""
    frames = [
        _scoped_frame(_REGION),
        _scoped_frame("elsewhere"),  # a region nobody here has named
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


def _walk(cols: int, rows: int) -> Screen:
    topo, contacts = _walk_topo()
    return WalkScreen(
        session=_GallerySession(cols, rows),
        topo=topo,
        contacts=contacts,
        self_label="Homestead",
    )


def _timemachine(cols: int, rows: int) -> Screen:
    def build(window, width):  # noqa: ANN001
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
    """A message flooded under a known region: its scope rides the title, stated once."""
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
    """A message flooded under a region nobody here has named: the code stands in."""
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
    """The minimal AppContext surface :class:`CourierOutboxScreen` actually reads."""

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
    """A record with positioned hops, turned to its Area tab: the drawing at the stage's size."""
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
    """A record turned to its Route tab: the graph, its key, the route line, the trace row."""
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
    """A channel text flooded under a region: the route row names it."""
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
    """A channel text flooded under a region nobody here has named: its code stands in."""
    return _packet_viewer_scoped(cols, rows, region="elsewhere")


def _trace(cols: int, rows: int) -> Screen:
    async def _noop_flow(current):  # noqa: ANN001
        return current

    async def _trace_call(path_spec, on_trace):  # noqa: ANN001
        pass  # never invoked — the screen is seeded directly via _on_trace below

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


#: A device snapshot shaped like a companion's, for the Device info page. Every field the
#: config table draws, including the pairing PIN it conceals.
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
    # The same page after ^S: the widest state of the row the other case masks.
    screen = _device_info(cols, rows)
    screen.handle("reveal")
    return screen


def _config_editor(cols: int, rows: int) -> Screen:
    """The Device config editor: every setting staged from one grouped list."""
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
    """The repeater admin page at its widest: values read, one staged, Apply drawn."""
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
    """The Preferences page at its widest: one saved override, one staged, reset row drawn."""
    prefs = Preferences()
    prefs.set("trace_cooldown_s", 2.5)
    title, items = _preference_items(prefs, {"history_days": 90})
    return SelectScreen(
        title,
        items,
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
    )


def _preferences_weekly_advert(cols: int, rows: int) -> Screen:
    """The Preferences page with the weekly advert on and its longest status line drawn."""
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
    """The transmit-cooldown countdown at its widest: a flood advert's wait and its reason.

    A dialog rather than a menu screen, and here for the one thing only this harness does:
    its box is sized from its own content, so a longer reason line would overflow the
    PicoCalc's 53 columns with nothing else to catch it.
    """
    return CountdownDialog("Flood advert", 47.0, reason="a flood advert reaches the whole mesh")


def _about_meshterm(cols: int, rows: int) -> Screen:
    return AboutPage("About MeshTerm", about_meshterm())


def _about_author(cols: int, rows: int) -> Screen:
    return AboutPage("About the author", about_author())


def _support_project(cols: int, rows: int) -> Screen:
    return AboutPage("Support MeshTerm", support_project())


def _join_discord(cols: int, rows: int) -> Screen:
    return AboutPage("Join Discord", join_discord())


def _diagnostics(cols: int, rows: int) -> Screen:
    """The Diagnostics page, carrying the widest values a real one can.

    A deep Windows config directory and a connection error in the radio's own words are the
    two fields with no natural bound, and both are here on purpose: a markdown list item
    hangs its wrapped text under itself at any width, and that is what has to survive 53
    columns.
    """
    from meshterm.ui.diagnostics import DiagnosticsPage

    return DiagnosticsPage(
        _diagnostics_source(),
        session=_GallerySession(cols, rows),
        save_path=Path("C:/Users/somebody/.meshterm/meshterm-diagnostics.md"),
    )


def _diagnostics_source() -> str:
    """A representative diagnostics document, built the way the tool builds one."""
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


def _share_qr(cols: int, rows: int) -> Screen:
    """The share screen: a contact card's code and link on a bare frame (the widest QR)."""
    from meshterm.ui.qr import QrScreen

    url = "meshcore://contact/add?name=Lakeside&public_key=" + "ab" * 32 + "&type=2"
    return QrScreen(url, title="Share Lakeside")


_ENTRIES: list[_Entry] = [
    _Entry("dashboard", _dashboard),
    _Entry("contacts", _contacts),
    _Entry("archive_ladder", _archive_ladder),
    _Entry("archive_preview", _archive_preview),
    _Entry("archived", _archived),
    _Entry("node_detail", _node_detail),
    _Entry("node_detail_map", _node_detail_map),
    _Entry("map", _map),
    _Entry("map_panned", _map_panned),
    _Entry("map_find", _map_find),
    _Entry("chat", _chat),
    _Entry("chat_picker", _chat_picker),
    _Entry("channels_manager", _channels_manager),
    _Entry("channel_detail", _channel_detail),
    _Entry("livefeed", _livefeed),
    _Entry("livefeed_scopes", _livefeed_scopes),
    _Entry("walk", _walk),
    _Entry("timemachine", _timemachine),
    _Entry("message_paths", _message_paths),
    _Entry("message_paths_scoped", _message_paths_scoped),
    _Entry("message_paths_unknown_scope", _message_paths_unknown_scope),
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
    _Entry("preferences", _preferences),
    _Entry("preferences-weekly-advert", _preferences_weekly_advert),
    _Entry("cooldown_countdown", _cooldown_countdown),
    _Entry("about_meshterm", _about_meshterm),
    _Entry("about_author", _about_author),
    _Entry("join_discord", _join_discord),
    _Entry("support_project", _support_project),
    _Entry("share_qr", _share_qr),
    _Entry("diagnostics", _diagnostics),
]

#: (platform, cols, rows) combos every entry above renders under. PicoCalc gets both its
#: live floor (53x26, the on-device measurement — see the plan's P0 appendix) and the lux
#: case (53x40, today's boot fbcon font / a future 6x8 font). Regular is unchanged by this
#: seam's arrival, so it stays the existing 72x24 standard.
_COMBOS: list[tuple[Platform, int, int]] = [
    (REGULAR, REGULAR.readable_cols, REGULAR.readable_rows),
    (PICOCALC, PICOCALC.readable_cols, PICOCALC.readable_rows),
    (PICOCALC, PICOCALC.readable_cols, 40),
]

#: Entries that overflow PICOCALC's 53 columns today (a width overflow doesn't depend on
#: row count, so one entry here covers both picocalc combos). P6 emptied it — the whole
#: P1 worklist graduated once the F-key lane replaced the per-screen hint strings and the
#: path composer's wrapped empty-state note stopped smuggling a newline into one row —
#: so every picocalc case is now a hard gate. A new screen that can't fit 53 goes here
#: only with a ticket, never to stay.
_KNOWN_WIDE: set[str] = set()

#: Entries whose footer_hint already overflowed the *existing* 72-column standard before
#: this platform seam existed. Not this phase's to fix: CLAUDE.md's 72-col rule predates
#: the seam, and per standing guidance old chrome that already broke it is left for a
#: dedicated pass rather than retrofitted as a drive-by here. Empty since the F-lane pass
#: of 2026-08-08 rebuilt the map's hint around its new view-jump keys and brought it back
#: inside the budget on the way through.
_PREEXISTING_REGULAR_OVERFLOW: set[str] = set()


#: The specimens by name, for a test that wants one entry rather than the whole sweep.
_ENTRY_BY_NAME = {entry.name: entry for entry in _ENTRIES}


def _cases():
    for entry in _ENTRIES:
        for platform, cols, rows in _COMBOS:
            case_id = f"{entry.name}-{platform.name}-{cols}x{rows}"
            marks = []
            if platform.name == "picocalc" and entry.name in _KNOWN_WIDE:
                marks.append(
                    pytest.mark.xfail(
                        reason=(
                            f"{entry.name} overflows picocalc's {PICOCALC.readable_cols} cols "
                            "today -- see _KNOWN_WIDE, the width-reduction worklist"
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
    """Assert every line of already-rendered output is within ``cols`` display cells."""
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
    """Every gallery specimen renders within its platform's width, raw and framed alike."""
    set_platform(platform)
    screen = entry.factory(cols, rows)
    screen.note_viewport(max(1, rows - 4))  # mirrors compose_base's own viewport math

    _assert_fits(screen.render_body(cols), cols, "render_body")
    # Assert the footer that is actually drawn on this platform. Regular draws each
    # screen's footer_hint string (which may carry Rich markup — map's
    # "[warn]offline[/warn]" — so parse before measuring). PicoCalc never draws the
    # hint strings at all: the fixed F-key lane replaces them (Platform.footer_fkeys),
    # so what must fit there is the screen's lane.
    if platform.footer_fkeys:
        lane = fkeys.lane_text(screen.fkey_lane)
        shifted = fkeys.lane_text(screen.fkey_lane, shifted=True)
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

    # No screen carries an exit row. Esc leaves — it is on both platforms' keyboards and
    # every footer_hint says so — and a row repeating it cost two lines of every screen,
    # which on the PicoCalc's 26 is a row in thirteen. The one surviving "Back" is the
    # staged-changes discard half ("✗ Back — discard …"), which is a choice rather than an
    # exit and never reads as a bare word.
    for line in _plain(screen.render_body(cols)).splitlines():
        assert line.strip() != "Back", f"{entry.name}: an exit row came back: {line!r}"

    # P3 assertions, on the *rendered ANSI* (the theme/fold contracts, not the config):
    # picocalc output may carry no truecolor or 256-colour SGR (the console has 16 slots,
    # addressed as plain 30-37/90-97/40-47 codes), and no character outside the 512-glyph
    # console font. Together these are the parity gate that catches a stray emoji or hex
    # colour the moment a screen grows one, instead of as tofu found on-device.
    if platform.name == "picocalc":
        for where, ansi_lines in (
            ("render_body", screen.render_body(cols)),
            ("compose_base", composed.split("\n")),
        ):
            for i, line in enumerate(ansi_lines):
                assert "[38;2;" not in line and "[48;2;" not in line, (
                    f"{entry.name} {where} line {i} emits truecolor SGR: {line!r}"
                )
                assert "[38;5;" not in line and "[48;5;" not in line, (
                    f"{entry.name} {where} line {i} emits 256-colour SGR: {line!r}"
                )
                strays = {ch for ch in line if ord(ch) >= 0x20 and ord(ch) not in FONT_CODEPOINTS}
                assert not strays, (
                    f"{entry.name} {where} line {i} has characters outside the console "
                    f"font: {sorted(strays)!r} in {line!r}"
                )


#: The map specimens and the credit each should be carrying — the whole OpenFreeMap line
#: on the map nobody has touched, the remnant on the two that have been used.
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
    """Every map specimen credits OpenStreetMap, on the surface its own frame affords.

    The width gate above would be perfectly happy with a map that had quietly stopped
    crediting anyone, so the specimens assert the mark itself as well: on a bordered frame
    it is set into the bottom border rule, right-justified with one rule cell before the
    corner, and the drawing is left alone; on the borderless one, which has no rule, it is
    stamped on the drawing's own last row. See :mod:`meshterm.ui.attribution`.
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
        # Rounded corners, or square where Rich judges the console legacy Windows (a
        # captured pytest run there) — either is the frame's own bottom rule.
        rule = next(ln for ln in reversed(lines) if "╰" in ln or "└" in ln)
        assert rule[-1] in "╯┘" and rule[:-1].endswith(f" {expected} ─"), rule
    else:
        assert body.splitlines()[-1].endswith(expected)
    assert any(expected in ln for ln in lines)
