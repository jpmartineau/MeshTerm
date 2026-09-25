# SPDX-License-Identifier: Apache-2.0
"""The packet viewer: one floating popup that can open any packet a list shows.

Everywhere MeshTerm lists packets (the dashboard feed today; any future packet list)
the rows share this module's per-kind chrome — a colour and a two-cell icon per packet
class — and every row can open into the same viewer: a centered dialog over the list
that lays the packet out in full, flavoured by kind. An advert shows the node's
identity, type, and location; telemetry shows the node and its reported values; an
RX-logged packet shows its parsed class and route — a flood's with the region it was
scoped to, if any (:func:`~meshterm.ui.widgets.scope_text`) — what it was addressed to (the
recipient and sender read out of the frame body — see :mod:`~meshterm.core.frames` — or
the token a trace or an ack stands on), plus the relay path it rode in on —
as a ``via`` chain on THE path line (:mod:`~meshterm.ui.pathline`), wrapping at hop
boundaries under its own lane, and, when it actually crossed a relay, as THE route graph
(:mod:`~meshterm.ui.pathgraph`): origin → relays → us, the same layered picture the
Message paths dialog and the Trophy case draw — and, for an overheard channel-text frame
naming a channel we hold the key for, the decrypted text too (see
:func:`~meshterm.core.channels.decrypt_channel_text`), even though the radio itself never
decoded it for us; a message shows the sender, the
conversation, and the text; an ack shows its code. Common to all: the class headline
(icon + UPPERCASE class, first row of the card), the timestamp, the node (under its
shared node-type mark, name coloured by the app-wide palette, key in the key widget),
reception quality on the shared SNR
bar, and any leftover raw field the flavoured layout doesn't already show.

When opened over a list the viewer pages through it in place — ``↑``/``↓`` step to
the newer/older packet, Home/End jump to the newest/oldest, mirroring the keys the
opening list itself walks rows with — so a burst can be read packet by packet
without bouncing back out to the list. PgUp/PgDn scroll a tall packet inside the
dialog, matching what they do on every other screen; Esc closes it.

Over a *live* list — one that hands the viewer a ``source`` — the top is **two stops
rather than one**, exactly as the live feed's own top is. ``↑`` off the newest packet,
and Home from anywhere, lands on the **pin**: the view holds the top *position* instead
of a packet, so every arrival becomes the card being read, and the title says
``following`` where it would say ``n/total``. ``↓`` steps back off the pin onto whichever
packet is showing at that moment, which then rides down the list as newer ones arrive —
the reader who opened a packet to read it keeps it, and the reader who walked up to the
top gets the stream. Same two stops, same keys, as the feed underneath.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from rich.console import RenderableType
from rich.table import Table
from rich.text import Text

from ..core.channels import decrypt_channel_text, identify_channel
from ..core.models import Observation, node_type_label, utcnow
from ..core.regions import Scope, frame_scope
from ..services.trace_runner import NodeResolver
from .marks import SELF_MARK, UNKNOWN_MARK
from .menus import SEP_COMPACT, SEP_ROOMY
from .pathgraph import PathLayer, render_path_graph, revisited_hops
from .pathline import PathLine, hops_atom, path_line
from .theme import glyph, name_style, snr_style
from .trace_screen import snr_bar
from .tui.render import render_lines, render_to_ansi
from .tui.screen import Screen
from .widgets import (
    DEFAULT_GLYPH,
    NODE_GLYPHS,
    NameKeyResolver,
    TypeOf,
    age_seconds,
    format_ago,
    highlighted_hash,
    node_type_legend,
    revisit_note,
    route_graph_style,
    scope_text,
)

#: Reads a frame's scope from its raw payload — ``ctx.region_store.scope_of`` in the app:
#: ``None`` for a frame with no scope to state (direct, or a route type never kept).
ScopeOf = Callable[[dict | None], Scope | None]


def unnamed_scope(raw: dict | None) -> Scope | None:
    """A frame's scope read without any region names — the fallback when none are at hand.

    Still says everything the frame itself says: a plain flood is ``unscoped``, and a
    scoped one is ``unknown scope <code>``. Only the *naming* needs the known regions (see
    :meth:`~meshterm.core.region_store.RegionStore.scope_of`), so a viewer opened without a
    store never draws less than the truth, only less than the whole of it.
    """
    return frame_scope(raw, ()) if isinstance(raw, dict) else None


#: Composed packet cards the viewer memoizes (a few pages either side of the view).
_BODY_CACHE_MAX = 8

#: Friendly gloss for a raw packet's parsed payload class (mirrors the meshcore
#: library's own ``PAYLOAD_TYPENAMES``), shown on a ``packet`` entry's "class" row.
_PAYLOAD_GLOSS = {
    "REQ": "request",
    "RESPONSE": "response",
    "TEXT_MSG": "direct message",
    "ACK": "ack",
    "ADVERT": "advert",
    "GRP_TXT": "channel text",
    "GRP_DATA": "channel data",
    "ANON_REQ": "anon request",
    "PATH": "path",
    "TRACE": "trace",
    "MULTIPART": "multipart",
    "CONTROL": "control",
}

#: Raw-payload fields already folded into a flavoured row elsewhere (frame plumbing
#: parsed into "class"/"route", the channel crypto handled by
#: :meth:`PacketViewer._decrypt_rows`, or an advert field duplicating ``node``/
#: ``lat``/``lon``/etc.) — skipped so the generic dump doesn't repeat them.
# fmt: off
_RAW_ROW_SKIP = frozenset({
    "node", "name", "kind", "snr", "rssi", "lat", "lon", "path", "node_type",
    "observed_at", "text",
    "header", "payload_ver", "transport_code", "scope_body", "path_len", "path_hash_size",
    "payload_type", "payload_typename", "route_type", "route_typename",
    "pkt_payload", "pkt_hash", "raw_hex", "payload", "payload_length", "recv_time",
    "chan_hash", "cipher_mac", "crypted", "message", "msg_hash", "sender_timestamp",
    "attempt", "txt_type",
    "dest_hash", "src_hash", "src_key", "trace_tag", "ack_crc", "trace_snrs",
    "adv_key", "adv_name", "adv_type", "adv_lat", "adv_lon",
})
# fmt: on

#: Display style per packet class, shared by every packet list and the viewer.
KIND_STYLES = {
    "advert": "accent",
    "telemetry": "brand",
    "packet": "muted",
    "message": "ok",
    "ack": "faint",
}

#: The two-cell icon per packet class — every icon is emoji-wide, so a list can swap
#: the textual kind label for the icon alone on a narrow terminal without the lanes
#: shifting. The fallback marks a class this table has never heard of.
KIND_ICONS = {
    "advert": "📢",
    "telemetry": "📊",
    "packet": "📦",
    "message": "💬",
    "ack": "✅",
}
DEFAULT_ICON = "❔"

#: The two-cell icon per parsed payload class of a raw ``packet`` frame, shared by the
#: viewer's leading class row and the dashboard's traffic meters. One glyph per concept:
#: 📻 rhymes with the Channels tool, 🎯 with Trace, and a raw ADVERT/ACK frame reuses
#: its kind's icon; :data:`DEFAULT_ICON` marks a typename this table has never heard of.
PAYLOAD_ICONS = {
    "REQ": "📥",
    "RESPONSE": "📮",
    "TEXT_MSG": "📩",
    "ACK": "✅",
    "ADVERT": "📢",
    "GRP_TXT": "📻",
    "GRP_DATA": "💽",
    "ANON_REQ": "🎭",
    "PATH": "🧭",
    "TRACE": "🎯",
    "MULTIPART": "🧩",
    "CONTROL": "🧰",
}

#: The colour a relayed packet's route draws in on THE route graph. It is the only path
#: on the canvas, so white just reads as "the route the packet rode" (the Trophy case's
#: single-walk convention).
_ROUTE_EDGE = (255, 255, 255)


@dataclass(slots=True)
class PacketEntry:
    """One listed packet, in the shape every packet list stores and the viewer reads.

    A deliberately flat union of what the feed's three event families carry — the
    monitor's observations, chat messages, and delivery acks — so one list type can
    hold them all and the viewer can flavour by :attr:`kind`.

    Attributes:
        when: When the packet was heard.
        kind: Packet class (``advert``/``telemetry``/``packet``/``message``/``ack``).
        node: The transmitting node's stored hash, when identified.
        name: The name the packet itself carried (an advert's node name, a message's
            sender), if any; display falls back to resolving :attr:`node`.
        snr: Reception SNR in dB, if measured (for ``packet`` rows: of the *last relay*).
        rssi: Reception strength in dBm, if measured.
        lat: Advertised latitude, when the packet shared a location.
        lon: Advertised longitude, when the packet shared one.
        node_type: The advertised node type (see ``NODE_TYPE_*``), if carried.
        path: A ``packet`` row's relay path: comma-separated hop hashes in propagation
            order (empty = arrived direct; ``None`` = the class carries no path).
        where: A message's conversation (``ch 3`` / ``direct``) or an ack's code.
        channel: A channel message's slot index — the fact behind ``where``'s prose, kept
            apart from it so a list can name the channel rather than number it.
        text: A message's body, when it is known.
        raw: The raw event payload, for the values a class-specific layout can't name.
    """

    when: datetime
    kind: str
    node: str | None = None
    name: str | None = None
    snr: float | None = None
    rssi: float | None = None
    lat: float | None = None
    lon: float | None = None
    node_type: int | None = None
    path: str | None = None
    where: str | None = None
    channel: int | None = None
    text: str | None = None
    raw: dict | None = None

    @classmethod
    def from_observation(cls, obs: Observation) -> PacketEntry:
        """Capture an overheard observation (the monitor's event family) as an entry."""
        return cls(
            when=obs.observed_at,
            kind=obs.kind,
            node=obs.node,
            name=obs.name,
            snr=obs.snr,
            rssi=obs.rssi,
            lat=obs.lat,
            lon=obs.lon,
            node_type=obs.node_type,
            path=obs.path,
            raw=obs.raw,
        )


def kind_icon(kind: str) -> str:
    """The icon for a packet class (a fallback for unknown classes).

    Returns the emoji on REGULAR; on PICOCALC, the emoji is mapped to a single-cell
    glyph from the 512-glyph font via :func:`~meshterm.ui.theme.glyph`.
    """
    emoji = KIND_ICONS.get(kind, DEFAULT_ICON)
    return glyph(emoji)


def payload_class(raw: dict | None) -> str | None:
    """The friendly payload-class label for a raw ``packet`` frame, or ``None`` if unknown.

    Maps the frame's ``payload_typename`` (``GRP_TXT``, ``TRACE``, …) through
    :data:`_PAYLOAD_GLOSS` — the one identifying thing a relayed flood carries when it
    names no origin node, so a packet list can read "channel text" / "trace" instead of a
    bare ``?``.
    """
    if not isinstance(raw, dict):
        return None
    typename = raw.get("payload_typename")
    if not typename:
        return None
    return _PAYLOAD_GLOSS.get(typename, typename.lower())


def class_marks(entry: PacketEntry) -> tuple[str, str]:
    """What a packet actually *is*: its icon and its plain-case class label.

    The one place the app decides which class a listed packet belongs to, so the viewer's
    headline and the feed's class lane can never disagree. A raw ``packet`` frame's class
    is its *parsed payload* class — ``channel text``, ``trace``, … — under its
    :data:`PAYLOAD_ICONS` glyph, because ``packet`` alone names only the event family the
    frame arrived in, not the thing it carries; a typename this table has never heard of
    keeps the fallback mark and shows the raw typename; a class-less frame stays a plain
    ``packet``. Every other kind is its own class (``advert``, ``message``, …) under the
    shared :data:`KIND_ICONS` glyph. The emoji are mapped to single glyphs on PICOCALC.
    """
    if entry.kind == "packet":
        raw = entry.raw if isinstance(entry.raw, dict) else {}
        typename = raw.get("payload_typename")
        if typename:
            emoji = PAYLOAD_ICONS.get(typename, DEFAULT_ICON)
            return glyph(emoji), _PAYLOAD_GLOSS.get(typename, typename.lower())
        return glyph(KIND_ICONS["packet"]), "packet"
    return kind_icon(entry.kind), entry.kind


def class_chrome(entry: PacketEntry) -> tuple[str, str]:
    """The class headline the viewer's card leads with — :func:`class_marks`, shouted.

    The card's first row is a headline, so it takes the UPPERCASE treatment; a list lane
    reads the same class in its own plain case straight from :func:`class_marks`.
    """
    icon, label = class_marks(entry)
    return icon, label.upper()


def node_label(
    entry: PacketEntry, resolve: NodeResolver, self_name: str | None = None
) -> tuple[str, str]:
    """The display name and style for an entry's node — the app-wide naming rule.

    What ``resolve`` knows about the hash wins (the contact list is the canonical
    identity), then the name the packet itself carried; a genuinely nameless node
    shows its hash. Names take their stable palette hue
    (:func:`~meshterm.ui.theme.name_style`) — except our own node, which is always
    the pure-white ``you`` — and a bare hash stays muted, because colour is the
    "this is a name" signal.

    Args:
        entry: The packet whose node to label.
        resolve: Maps a node hash to a friendly name when known.
        self_name: Our own node's name, to spot "us" (``None`` = never matches).

    Returns:
        ``(label, style)``, ready to append to a row.
    """
    name = None
    if entry.node:
        resolved = resolve(entry.node)
        if resolved and resolved != entry.node:
            name = resolved
    if not name:
        name = entry.name
    if not name:
        return (entry.node or "?", "muted")
    if self_name and name == self_name:
        return (name, "you")
    return (name, name_style(name, entry.node))


@dataclass(frozen=True, slots=True)
class _Reception:
    """The card's reception row — SNR with its quality bar, then RSSI — sized to its lane.

    Handed to the grid *unsized*, like the via path beside it: the value column's width is
    only settled once the label lane is (see :meth:`PacketViewer._label_width`), so the row
    lays itself out at render time. The two atoms chain on the app's roomy separator and
    fall back to the compact one (:data:`~meshterm.ui.menus.SEP_ROOMY`) when the roomy form
    would not fit — the header's own trade, made wherever the cells are the scarce thing.
    On the 53-column console they are: a two-digit SNR beside a three-digit RSSI misses the
    lane by exactly that padding, and folding put the bare word ``rssi`` on a line of its
    own (JP, on-device, 2026-08-10).

    Attributes:
        snr: Signal-to-noise ratio in dB, if measured.
        rssi: Reception strength in dBm, if measured.
    """

    snr: float | None
    rssi: float | None

    def _line(self, separator: str) -> Text:
        """The row drawn with ``separator`` between the two atoms."""
        line = Text()
        if self.snr is not None:
            line.append(f"{self.snr:+.1f} dB  ", style=snr_style(self.snr))
            line.append_text(snr_bar(self.snr))
        if self.rssi is not None:
            if self.snr is not None:
                line.append(separator, style="muted")
            line.append(f"{self.rssi:.0f} dBm rssi", style="muted")
        return line

    def __rich_console__(self, console, options):
        roomy = self._line(SEP_ROOMY)
        yield roomy if roomy.cell_len <= options.max_width else self._line(SEP_COMPACT)


class PacketViewer(Screen):
    """The floating packet dialog: one entry laid out in full, pageable over its list.

    A read-only popup. The opener hands it the list *as rendered* (newest first) and
    the index of the row the user opened; ``↑``/``↓`` then walk toward newer/older
    packets — the same keys the opening list itself uses — Home/End jump to the
    ends, PgUp/PgDn scroll a tall body, Esc closes. Opened over a single packet (a
    one-entry list) the paging keys simply do nothing.

    Over a live list (``source``) the top gains the feed's second stop — the pin, where
    the view follows the stream instead of a packet (see the module docstring and
    :meth:`_pin`). Opened over a snapshot there is nothing to follow, and ``↑`` on the
    newest packet clamps as it always did.

    The dialog only ever grows (see
    :attr:`~meshterm.ui.tui.screen.Screen.grow_only`): paging from a short packet to a
    tall one enlarges the box, but paging back keeps it at that size — blank-padded
    below the shorter body — rather than re-centering smaller, so the header the reader
    is looking at never hops around as they page.
    """

    grow_only = True

    @property
    def fkey_lane(self):
        """The shared pager over the body, with the jumps renamed for what they land on.

        Home and End here don't reach the ends of a *body* — they reach the ends of the
        *list*, the newest and oldest packet — so the Shift companions say ``Newest`` and
        ``Oldest``. The handedness is unchanged: newest is the top of a newest-first list,
        so it keeps the right-hand slot behind Page ↑.

        The two banks gate on different things, because they move different things. The
        pager needs a body taller than the box; the jumps need somewhere to jump to — a
        second packet, or, on a live list, the pin that ``Newest`` reaches even when the
        list holds one packet (the feed's ``Top`` chip, one screen up). Opened over a lone
        packet of a snapshot, all four chips go dim at once.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self.content_overflows))
        many = len(self._entries) > 1 or self._can_pin
        lane[3] = FPair(
            "Page ↓", "pagedown", "Oldest", "end", enabled=self.content_overflows, opp_enabled=many
        )
        lane[4] = FPair(
            "Page ↑", "pageup", "Newest", "home", enabled=self.content_overflows, opp_enabled=many
        )
        return lane

    def __init__(
        self,
        entries: list[PacketEntry],
        index: int,
        *,
        resolve: NodeResolver,
        prefix_bytes: int = 0,
        self_name: str | None = None,
        on_navigate: Callable[[PacketEntry], None] | None = None,
        on_pin: Callable[[], None] | None = None,
        channels: Sequence[tuple[str, bytes]] = (),
        source: Callable[[], Sequence[PacketEntry]] | None = None,
        type_of: TypeOf | None = None,
        key_of: NameKeyResolver | None = None,
        scope_of: ScopeOf | None = None,
    ) -> None:
        """Open the viewer over a packet list.

        Args:
            entries: The list's packets, newest first — the opening snapshot.
            index: Which entry to open on.
            resolve: Maps a node hash to a friendly name when known.
            prefix_bytes: Path-hash width to light in displayed hashes (0 = none).
            self_name: Our own node's name, drawn white wherever it appears.
            on_navigate: Called with the newly shown entry whenever paging moves the
                view, so the opening list can walk its own highlight in step.
            on_pin: Called when the view moves onto the pin, so a list that has the same
                stop (the live feed) can follow the stream too and still be following it
                when the dialog closes. Never called without a ``source``: the pin is
                only a stop where the list is live.
            channels: The device's configured channels, as ``(name, secret)`` pairs —
                tried against an overheard ``packet`` entry's channel-text frame (see
                :func:`~meshterm.core.channels.decrypt_channel_text`), so a raw frame
                the radio never decoded for us can still be read when we hold the key.
            source: Optional zero-arg callable returning the list's *current* packets
                (newest first). When given, the viewer re-reads it every repaint and
                keypress and re-locates the packet being viewed by identity, so packets
                that arrive while the dialog is open become reachable (``↑`` walks up
                into them) instead of the view being frozen at its opening snapshot.
            type_of: Maps a relay hash to its node type, so a relayed packet's route
                graph marks a repeater ``▲`` (etc.) instead of a generic dot; ``None``
                keeps the plain named-dot / unknown-ring fallback.
            key_of: Maps an origin's display name back to its node's key, so the route
                graph's left endpoint takes its key-derived hue; ``None`` (or an
                unresolvable name) leaves it muted.
            scope_of: Reads a flood's scope off its raw frame against the regions known
                by name (``ctx.region_store.scope_of``), so the ``route`` row can say which
                region a scoped flood was sent into. ``None`` falls back to
                :func:`unnamed_scope`: the flood is still told apart as scoped or not,
                and a scoped one shows its code where a name would go.
        """
        super().__init__()
        self._entries = list(entries)
        self._index = max(0, min(index, len(self._entries) - 1))
        # Composed cards per (entry, width, age-minute) — see render_body.
        self._body_cache: OrderedDict[tuple, list[str]] = OrderedDict()
        self._resolve = resolve
        self._prefix_bytes = prefix_bytes
        self._self_name = self_name
        self._on_navigate = on_navigate
        self._on_pin = on_pin
        self._channels = channels
        self._source = source
        self._type_of = type_of
        self._key_of = key_of
        self._scope_of: ScopeOf = scope_of or unnamed_scope
        #: The packet currently shown, tracked by identity so a live prepend to the
        #: source (which shifts every index) never slides the view onto another packet.
        self._current: PacketEntry | None = self._entries[self._index] if self._entries else None
        #: Whether the view is on the *pin* — the stop above the newest packet, which
        #: holds the top position rather than a packet. Implies ``_index == 0``: the pin
        #: is a mode on the newest entry, so everything that reads the view (the card,
        #: the scroll, the cache) needs no special case; only the title and the moves
        #: differ. Opening never pins — Enter named *this* packet (see
        #: :meth:`~meshterm.ui.livefeed_screen.LiveFeedScreen._open_packet`) — so the
        #: reader walks up to the stream deliberately or not at all.
        self._pinned = False
        self._update_footer()
        self._set_title()

    def _update_footer(self) -> None:
        """Set the footer hint to the moves that are live right now.

        Three shapes, because the top of a live list is two stops. On the pin only ``↓``
        moves, and the atom says what stepping off it buys — the packet on screen, held
        against the next arrival. One stop below, ``↑`` and ``↓`` do different things (one
        follows the stream, the other walks older), so they take an atom each rather than
        sharing ``newer/older``, which would name neither. Everywhere else the pair walks
        the list and reads as one atom, and a lone packet with nothing to follow keeps the
        bare ``Esc close`` it always had.
        """
        if self._pinned:
            atoms = ["↓ hold packet"]
        elif self._index == 0 and self._can_pin:
            atoms = ["↑ follow"] + (["↓ older"] if len(self._entries) > 1 else [])
        elif len(self._entries) > 1:
            atoms = ["↑↓ newer/older"]
        else:
            atoms = []
        if len(self._entries) > 1 or self._can_pin:
            atoms += ["PgUp/PgDn scroll", "Home/End ends"]
        atoms.append("Esc close")
        self.footer_hint = " · ".join(atoms)

    @property
    def _can_pin(self) -> bool:
        """Whether this viewer has a stream to follow — which is what a ``source`` is.

        Over a snapshot there is no second stop above the newest packet: nothing will ever
        arrive there, so the pin would be an empty mode and ``↑`` clamps instead.
        """
        return self._source is not None

    def _sync(self) -> None:
        """Refresh the entries from the live source, re-finding the viewed packet by identity.

        A no-op without a ``source``. Otherwise the list is re-read (picking up any
        packets that arrived since the last paint) and the view stays on the *same*
        packet object — its index simply moves as newer packets prepend ahead of it —
        so paging keys measure against the current list and newly arrived packets sit
        reachable above the one being read. A packet that has since aged out of the
        list drops the view to the nearest surviving index.

        The pin is where that parts company: it is attached to the *position*, so a
        re-read lands it on whatever is newest now and the card under it changes. This
        runs on every paint as well as before every move, so following costs nothing
        more than the repaint the feed was already buying.
        """
        if self._source is None:
            return
        entries = list(self._source())
        if not entries:
            return
        self._entries = entries
        if self._pinned:
            self._show(0)
        elif self._current is not None:
            for i, candidate in enumerate(entries):
                if candidate is self._current:
                    self._index = i
                    break
            else:
                self._index = min(self._index, len(entries) - 1)
                self._current = entries[self._index]
        else:
            self._index = min(self._index, len(entries) - 1)
            self._current = entries[self._index]
        self._update_footer()
        self._set_title()

    def _set_title(self) -> None:
        # No emoji in a dialog title (the standards' rule); the class row carries the icon.
        # The position atom says which of the two things the view is doing: a count while
        # it holds a packet, and ``following`` while it holds the top of the stream — the
        # word rather than a perpetual ``1/n``, because what changes under the pin is the
        # card, not the number.
        entry = self._entries[self._index]
        title = entry.kind
        if self._pinned:
            title += " · following"
        elif len(self._entries) > 1:
            title += f" · {self._index + 1}/{len(self._entries)}"
        self.title = title

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Page through the list, follow the stream, scroll a tall entry, or dismiss."""
        if action in ("up", "down", "home", "ctrl_home", "end", "ctrl_end"):
            # Re-read the live list first so newly arrived packets are in range before we
            # step — otherwise the first ``↑`` off the newest could never reach them.
            self._sync()
        if action == "up":
            # Off the newest packet ``↑`` has one more place to go on a live list: the
            # pin. On a snapshot the same press clamps, there being no stream to follow.
            if self._index == 0:
                self._pin()
            else:
                self._jump(self._index - 1)
        elif action == "down":
            # Off the pin ``↓`` does not move down a row — it lands on the same packet
            # showing under it, now held. The feed's own ``↓`` off its pin, exactly.
            self._jump(self._index if self._pinned else self._index + 1)
        elif action in ("home", "ctrl_home"):
            # "Take me back to the top" resumes the stream where there is one, rather than
            # parking on whichever packet happens to be newest this instant — the feed's
            # Home again, and what the ``Newest`` chip reaches on the console.
            if self._can_pin:
                self._pin()
            else:
                self._jump(0)
        elif action in ("end", "ctrl_end"):
            self._jump(len(self._entries) - 1)
        elif action == "pageup":
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self.scroll_pages(1)
        elif action in ("escape", "enter"):
            self.resolve(None)

    def _jump(self, index: int) -> None:
        """Hold the entry at ``index`` (clamped), leaving the pin behind.

        Landing on an entry is by definition holding a packet rather than following the
        stream, so this is also the way *off* the pin — and the one move that does
        something while the index stays put, which is exactly what ``↓`` off the pin is.
        The scroll resets only where the packet itself changes (see :meth:`_show`), so
        that press keeps the reader where they were in the card they were already reading.
        """
        index = max(0, min(index, len(self._entries) - 1))
        if index == self._index and not self._pinned:
            return
        self._pinned = False
        self._show(index)
        self._set_title()
        self._update_footer()
        if self._on_navigate is not None and self._current is not None:
            self._on_navigate(self._current)

    def _pin(self) -> None:
        """Follow the stream: hold the top of the list, whichever packet is newest.

        A no-op over a snapshot (nothing to follow), and on the pin already. Otherwise the
        list is re-read so the pin lands on what is newest *now*, and the opener is told —
        so a list carrying the same stop follows along, and is still following when the
        dialog closes back onto it.
        """
        if self._pinned or not self._can_pin:
            return
        self._pinned = True
        self._sync()
        self._set_title()
        self._update_footer()
        if self._on_pin is not None:
            self._on_pin()

    def _show(self, index: int) -> None:
        """Put the view on entry ``index``, starting a *different* packet from its top.

        The scroll belongs to the packet, not to the position: paging onto another card
        starts it at its beginning, while re-landing on the card already showing (``↓``
        off the pin) leaves the reader exactly where they had scrolled to.
        """
        self._index = index
        entry = self._entries[index]
        if entry is not self._current:
            self._current = entry
            self.scroll = 0

    # --- rendering -------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Lay the current entry out as labelled rows, flavoured by its kind.

        A relayed ``packet`` slots THE route graph (:mod:`~meshterm.ui.pathgraph`) between
        its parsed detail (heard/from/class/via) and its extras (location/text/raw): the
        via row names each hop precisely, the graph shows the same relay chain as a shape —
        origin at the left, us at the right. The two label/value grids around it share one
        label-lane width so their rows still line up across the picture.
        """
        # Fold in any packets that arrived since the last paint, keeping the view on the
        # same packet, so the title's ``n/total`` and the reachable range stay current.
        self._sync()
        entry = self._entries[self._index]
        # An entry is immutable once captured, so its whole card — rows, decrypted
        # payload, route-graph layout — is a pure function of the entry, the width,
        # and the minute its "heard" age reads (the one row that rolls with time).
        # Memoize on exactly that: the repaint tick re-lays the graph only when a
        # minute boundary actually moves the visible text.
        age_minute = int(max(0.0, (utcnow() - entry.when).total_seconds()) // 60)
        # …and of the flood's scope, which is *not* fixed at capture: a region learned
        # while the card is open (a repeater's list arriving, a name typed) names a code
        # that read as unknown a moment ago. The store memoizes the lookup, so asking it
        # every paint costs a dict hit.
        key = (id(entry), width, age_minute, self._entry_scope(entry))
        cached = self._body_cache.get(key)
        if cached is not None:
            self._body_cache.move_to_end(key)
            self._scroll_total = max(1, len(cached))
            return cached
        head, tail = self._head_rows(entry), self._tail_rows(entry)
        label_w = self._label_width(head + tail, width)

        lines = self._grid_lines(head, width, label_w)
        graph = self._graph_lines(entry, width)
        if graph:
            lines.append("")
            lines.extend(graph)
            caption = Text("origin → you · labels = hash byte", style="faint")
            lines.append(render_to_ansi(caption, width, no_wrap=True))
            lines.extend(render_lines(node_type_legend(width=width), width, no_wrap=True))
            if tail:
                lines.append("")
        lines.extend(self._grid_lines(tail, width, label_w))
        self._scroll_total = max(1, len(lines))
        self._body_cache[key] = lines
        if len(self._body_cache) > _BODY_CACHE_MAX:
            self._body_cache.popitem(last=False)
        return lines

    @staticmethod
    def _label_width(rows: list[tuple[str, RenderableType]], width: int) -> int:
        """The shared label-lane width so wrapped values hang under their own block.

        One fixed width for every grid on the card (the app-wide hanging-indent rule), never
        column zero — sized to the widest label present (a raw payload field's full name
        included, so none is clipped) plus a one-cell gutter, but never so wide it leaves the
        value column under 20 cells (a pathological key then ellipsises instead of crushing it).
        """
        widest = max((len(label) for label, _ in rows), default=0)
        return max(10, min(widest + 1, width - 20))

    @staticmethod
    def _grid_lines(rows: list[tuple[str, RenderableType]], width: int, label_w: int) -> list[str]:
        """Render one label/value grid to ANSI lines (empty rows → no lines)."""
        if not rows:
            return []
        grid = Table(
            box=None,
            show_header=False,
            show_edge=False,
            pad_edge=False,
            padding=(0, 0),
            expand=False,
        )
        grid.add_column(width=label_w, no_wrap=True)
        grid.add_column(overflow="fold", max_width=max(20, width - label_w))
        for label, value in rows:
            grid.add_row(Text(label, style="muted"), value)
        return render_lines(grid, width)

    def _head_rows(self, entry: PacketEntry) -> list[tuple[str, RenderableType]]:
        """The rows above the route graph.

        The class headline, the common core, then a packet's parsed frame.
        """
        rows: list[tuple[str, RenderableType]] = []

        icon, class_label = class_chrome(entry)
        rows.append(("class", Text(f"{icon} {class_label}")))

        secs = age_seconds(entry.when)
        heard = Text(entry.when.astimezone().strftime("%b %d %H:%M:%S"))
        heard.append(f"  ({format_ago(secs)})", style="muted")
        rows.append(("heard", heard))

        if entry.snr is not None or entry.rssi is not None:
            rows.append(("snr", self._reception(entry)))

        if entry.node or entry.name:
            who = Text()
            label, style = node_label(entry, self._resolve, self._self_name)
            glyph, glyph_style = self._node_marker(entry, style)
            who.append(glyph, style=glyph_style)
            who.append(" ")
            who.append(label, style=style)
            if entry.node and label != entry.node:
                who.append("  ")
                who.append_text(highlighted_hash(entry.node, self._prefix_bytes))
            rows.append(("from", who))
        if entry.node_type is not None:
            rows.append(("type", Text(node_type_label(entry.node_type))))

        if entry.kind == "packet":
            rows.extend(self._packet_rows(entry))
        return rows

    def _node_marker(self, entry: PacketEntry, style: str) -> tuple[str, str]:
        """The node-type mark leading the ``from`` row — the app's shared node glyphs.

        The same marks the map, the contact list and the route graph below plant on a
        node (``★`` us, ``▲`` repeater, ``■`` room, ``◉`` sensor, ``●`` node), so a
        packet's sender reads as the same *kind* of thing here as everywhere else — and
        the ``type`` row underneath spells out in words what the glyph says at a glance.
        The type comes from the packet itself when it carried one (an advert's), else
        from what the contacts know about its hash; a node neither can type keeps the
        ``○`` unknown ring rather than being passed off as a plain client.

        Args:
            entry: The packet whose sender is being marked.
            style: The style :func:`node_label` gave the name — ``"you"`` marks us.

        Returns:
            The ``(glyph, style)`` pair, ready to append to the row.
        """
        if style == "you":
            return SELF_MARK
        node_type = entry.node_type
        if node_type is None and entry.node and self._type_of is not None:
            node_type = self._type_of(entry.node)
        if node_type is None:
            return UNKNOWN_MARK
        return NODE_GLYPHS.get(node_type, DEFAULT_GLYPH)

    def _tail_rows(self, entry: PacketEntry) -> list[tuple[str, RenderableType]]:
        """The rows below the route graph: location, the message/ack fields, the raw dump."""
        rows: list[tuple[str, RenderableType]] = []
        if entry.lat is not None and entry.lon is not None:
            rows.append(("location", Text(f"{entry.lat:.5f}, {entry.lon:.5f}")))

        if entry.where:
            label = "code" if entry.kind == "ack" else "where"
            rows.append((label, Text(entry.where)))
        if entry.text:
            rows.append(("text", Text(entry.text)))

        rows.extend(self._raw_rows(entry))
        return rows

    def _graph_lines(self, entry: PacketEntry, width: int) -> list[str]:
        """Draw a relayed packet's origin → relays → us route on THE route graph.

        Only a ``packet`` frame that actually crossed a relay draws one — a direct
        arrival's ``via`` row (``direct — no relays``) already says all there is to say, so
        a two-node graph would waste the height. The relay chain is the packet's path
        (originator-nearest first), the left endpoint the origin when the frame named one
        (an advert's ``adv_key``; other classes arrive origin-less, drawn ``?``), the right
        endpoint always us — the same layered route-graph widget the Message paths dialog
        and the Trophy case draw, in a single white path.

        Drawn with ``allow_duplicate_nodes``, because this chain is *observed*: we did not
        compose it, its hops are named by a one-byte hash, and a repeat among them is as likely
        two colliding nodes as a genuine loop. Folding such a repeat into one marker would make
        a cycle the left-to-right flow cannot seat — the walk collapses into a pile of markers
        one column wide — so each visit takes its own marker and the chain draws in the order it
        was heard. The ``via`` row's :func:`~meshterm.ui.widgets.revisit_note` says why a name
        appears twice.
        """
        if entry.kind != "packet":
            return []
        hops = tuple(hop for hop in (entry.path or "").split(",") if hop)
        if not hops:
            return []
        glyph_of, label_of, label_rgb_of = route_graph_style(
            resolve=self._resolve,
            self_name=self._self_name,
            source=self._graph_source(entry),
            type_of=self._type_of,
            key_of=self._key_of,
        )
        return render_path_graph(
            [PathLayer(hops=hops, color=_ROUTE_EDGE, priority=3)],
            width,
            glyph_of=glyph_of,
            label_of=label_of,
            label_rgb_of=label_rgb_of,
            allow_duplicate_nodes=True,
        )

    def _graph_source(self, entry: PacketEntry) -> str | None:
        """The origin's display name for the graph's left endpoint, or ``None`` if unknown.

        The naming rule the ``from`` row follows (a resolved contact wins, then the name the
        frame carried), but a nameless origin returns ``None`` so the graph draws a plain
        ``?`` endpoint rather than passing a bare hash off as a named node.
        """
        if entry.node:
            resolved = self._resolve(entry.node)
            if resolved and resolved != entry.node:
                return resolved
        return entry.name or None

    @staticmethod
    def _reception(entry: PacketEntry) -> _Reception:
        """SNR (with the shared quality bar) and RSSI on one line — see :class:`_Reception`."""
        return _Reception(entry.snr, entry.rssi)

    def _via_path(self, entry: PacketEntry) -> PathLine:
        """A ``packet`` entry's relay chain as THE path line (:mod:`~meshterm.ui.pathline`).

        Handed to the grid *unsized*: the line renders itself into whatever cell width
        the value lane works out, folding at hop boundaries — never mid-name, never
        mid-chip — and hanging its continuations under the value block. The dialog has
        the rows to spend, so a long chain reads whole here, unlike the feed row behind
        it, which has to elide its middle to stay on one line.

        A trace with no hops is the one case where an empty chain does not mean a direct
        shot. A trace names its relays nowhere — it records a *reading* per hop instead
        (see :func:`~meshterm.core.frames.trace_link_snrs`) — so a walked trace arrives
        with an empty path and a full set of readings, and "direct — no relays" would be
        a flat contradiction of the ``links`` row two lines above. It says how far the
        packet got instead, which is the one thing those readings do establish.

        The chain is the route's *middle* — the ``path`` field names the relays that
        forwarded the frame and neither the node that sent it nor the one that received
        it — so both ends are drawn open (``from_origin``/``to_destination``): the
        chevron says the route carries on past the chip, where a flat end would claim
        the first relay was the origin.
        """
        return path_line(
            (entry.path or "").split(","),
            self._resolve,
            prefix_bytes=self._prefix_bytes,
            self_name=self._self_name,
            empty=self._empty_via(entry),
            from_origin=False,
            to_destination=False,
        )

    @staticmethod
    def _empty_via(entry: PacketEntry) -> str:
        """What an empty relay chain means for this frame's class."""
        readings = (entry.raw or {}).get("trace_snrs") if isinstance(entry.raw, dict) else None
        if readings:
            hops = len(readings)
            return (
                f"{hops} hop{'' if hops == 1 else 's'} walked "
                "— a trace logs each leg, not its relays"
            )
        return "direct — no relays"

    def _packet_rows(self, entry: PacketEntry) -> list[tuple[str, RenderableType]]:
        """What a raw ``packet`` entry has to say for itself.

        Its addressing, parsed class and route, and relay path — plus, for a channel
        frame naming a channel we hold the key for, its channel and plaintext.
        """
        raw = entry.raw if isinstance(entry.raw, dict) else {}
        rows: list[tuple[str, RenderableType]] = []
        typename = raw.get("payload_typename")  # class itself leads the card (class_chrome)
        rows.extend(self._addressing_rows(raw))
        route = raw.get("route_typename")
        if route:
            rows.append(("route", self._route_text(route, self._entry_scope(entry))))
        rows.append(("via", self._via_path(entry)))
        # The figure the chain above encodes but never states, hung under it as the app's
        # own hop atom — the same one the node page's routes and the trace scenarios carry
        # (:func:`~meshterm.ui.pathline.hops_atom`), so a count reads the same wherever a
        # path has one. Only where there is a chain to count: an empty ``path`` has already
        # been spelt out in words on the row above (:meth:`_empty_via`), and a second line
        # reading ``direct`` under ``direct — no relays`` would be the same answer twice.
        relays = [hop for hop in (entry.path or "").split(",") if hop]
        if relays:
            rows.append(("", hops_atom(len(relays))))
        # A chain that names one hop twice reads as a mistake until it is explained; the graph
        # below draws that hop twice too (see ``_graph_lines``), so the note covers both.
        revisits = revisit_note(
            revisited_hops([hop for hop in (entry.path or "").split(",") if hop]),
            self._resolve,
            prefix_bytes=self._prefix_bytes,
            self_name=self._self_name,
        )
        if revisits is not None:
            rows.append(("", revisits))
        if typename == "GRP_TXT":
            rows.extend(self._decrypt_rows(raw))
        elif typename == "GRP_DATA":
            # A datagram's body is not text, so there is nothing to decode for a reader —
            # but the same MAC that would authorise decrypting it names its channel.
            named = identify_channel(
                raw.get("chan_hash") or "",
                raw.get("cipher_mac") or "",
                raw.get("crypted") or "",
                self._channels,
            )
            if named is not None:
                rows.append(("channel", Text(named[0], style="brand")))
            elif raw.get("chan_hash"):
                rows.append(
                    (
                        "channel",
                        Text(f"unknown (hash {raw['chan_hash']})", style="muted"),
                    )
                )
        return rows

    def _entry_scope(self, entry: PacketEntry) -> Scope | None:
        """The scope of an entry's frame, or ``None`` where it has none to state.

        Only a raw ``packet`` frame carries the route type and transport codes a scope is
        read from; every other kind answers ``None`` and draws nothing.
        """
        if entry.kind != "packet" or not isinstance(entry.raw, dict):
            return None
        return self._scope_of(entry.raw)

    @staticmethod
    def _route_text(route: str, scope: Scope | None) -> Text:
        """The ``route`` row: how the frame was routed, and for a flood, where to.

        A flood reads ``flood`` and then its scope as a chained atom
        (:func:`~meshterm.ui.widgets.scope_text`) — ``flood · scope harbour``,
        ``flood · unknown scope 3fa1``, ``flood · unscoped`` — rather than the library's
        ``tc flood``, which named the wire mechanism (transport codes) and left the
        reader to know that it meant *scoped*, and to which region nobody could say.
        A scoped and an unscoped flood are one routing; the scope is what differs, so it
        is the scope that is spelled out.

        A direct frame keeps its route word alone. A repeater never region-filters a
        direct packet, so it has no scope, and a ``TC_DIRECT`` frame's codes are the
        "to nowhere" pair a contact share uses, not a region — a word there would claim a
        meaning the frame does not carry.
        """
        if scope is None:
            return Text(route.replace("_", " ").lower())
        text = Text("flood")
        text.append(" · ", style="muted")
        text.append_text(scope_text(scope))
        return text

    def _addressing_rows(self, raw: dict) -> list[tuple[str, RenderableType]]:
        """Who a frame was for, who it says it was from, and the token it carries.

        The fields :mod:`~meshterm.core.frames` reads out of the frame body, laid out as
        the card's own rows rather than left to the raw dump at the bottom — a recipient is
        a node, and a node belongs under a resolved name and THE hash widget, not as a bare
        hex value in a debug list. Each endpoint is named by one byte of its key, so the
        name is the same good guess a relay hop's is (and the lit hash beside it says
        exactly which byte was matched); an anonymous request's sender carries its whole
        key, which resolves outright.

        Args:
            raw: The frame's raw payload.

        Returns:
            The rows to slot ahead of the route/via block (empty for a class that
            addresses nothing).
        """
        rows: list[tuple[str, RenderableType]] = []
        dest = raw.get("dest_hash")
        if dest:
            rows.append(("to", self._endpoint(dest)))
        src = raw.get("src_key") or raw.get("src_hash")
        if src:
            rows.append(("from", self._endpoint(src)))
        if raw.get("trace_tag"):
            rows.append(("tag", Text(raw["trace_tag"], style="muted")))
        readings = raw.get("trace_snrs")
        if readings:
            rows.append(("links", self._link_readings(readings)))
        if raw.get("ack_crc"):
            # An ack identifies the message it answers, by that message's own checksum.
            rows.append(("acks", Text(raw["ack_crc"], style="muted")))
        return rows

    def _link_readings(self, readings: list[float]) -> Text:
        """How well each leg of a trace's walk was heard, in the order it walked them.

        A trace is the one class that reports on the mesh as it crosses it: every node
        that forwards it records the SNR it heard its predecessor at, so an overheard
        trace carries a reading per hop travelled — and how many there are says how far
        along its route the frame had got when we caught it. Read oldest hop first, the
        same direction a path line runs, each coloured by :func:`~meshterm.ui.theme.
        snr_style` like every other reception figure in the app.

        Args:
            readings: The per-hop SNRs in dB, in walk order.

        Returns:
            The readings as one separated line.
        """
        line = Text()
        for at, value in enumerate(readings):
            if at:
                line.append(SEP_COMPACT, style="muted")
            line.append(f"{value:+.2f} dB", style=snr_style(value))
        return line

    def _endpoint(self, value: str) -> Text:
        """One end of an addressed frame: its resolved name, then the key it was named by.

        The ``from`` row's own presentation, minus the node-type glyph — a one-byte hash
        is too thin an identity to plant a type mark on. A node we can't name shows the
        hash alone, grey whole (``known=False``) like every unknown-node hash in the app.
        """
        text = Text()
        named = self._resolve(value)
        known = bool(named and named != value)
        if known:
            style = (
                "you" if self._self_name and named == self._self_name else name_style(named, value)
            )
            text.append(named, style=style)
            text.append("  ")
        text.append_text(highlighted_hash(value, self._prefix_bytes or 1, known=known))
        return text

    def _decrypt_rows(self, raw: dict) -> list[tuple[str, RenderableType]]:
        """Try every known channel's key against an overheard channel-text frame.

        The frame names its channel only by a one-byte hash fingerprint (several
        channels can collide on it), so :func:`~meshterm.core.channels.
        decrypt_channel_text` confirms the match by MAC before trusting a key to
        decrypt anything — a channel we don't hold the key for, or a fingerprint we
        can't confirm, is reported as such rather than left silently missing.
        """
        chan_hash = raw.get("chan_hash")
        cipher_mac = raw.get("cipher_mac")
        crypted = raw.get("crypted")
        if not (chan_hash and cipher_mac and crypted):
            return []
        decrypted = decrypt_channel_text(chan_hash, cipher_mac, crypted, self._channels)
        if decrypted is None:
            return [
                (
                    "channel",
                    Text(f"unknown (hash {chan_hash}) — can't decrypt", style="muted"),
                )
            ]
        rows: list[tuple[str, RenderableType]] = [
            ("channel", Text(decrypted.channel_name, style="brand")),
        ]
        body = Text(decrypted.text)
        if decrypted.sent_at is not None:
            body.append(
                f"  · sent {decrypted.sent_at.astimezone().strftime('%H:%M:%S')}",
                style="muted",
            )
        if decrypted.attempt:
            body.append(f"  (resend #{decrypted.attempt})", style="muted")
        rows.append(("text", body))
        return rows

    def _raw_rows(self, entry: PacketEntry) -> list[tuple[str, RenderableType]]:
        """The raw payload's leftover fields, one labelled row each (telemetry's meat).

        Fields the flavoured layout already presents are skipped; the rest render as
        flat ``key value`` rows so a telemetry frame's channels (or a new firmware
        field the layout doesn't know yet) are still visible without a debugger.
        """
        raw = entry.raw if isinstance(entry.raw, dict) else None
        if not raw:
            return []
        shown = {k: v for k, v in raw.items() if k not in _RAW_ROW_SKIP and v is not None}
        if not shown:
            return []
        rows: list[tuple[str, RenderableType]] = [("", Text())]
        rows.append(("payload", Text(f"{len(shown)} raw fields", style="faint")))
        for key, value in list(shown.items())[:12]:
            body = str(value)
            if len(body) > 200:
                body = body[:199] + "…"
            # The label lane widens to fit the full key (see :meth:`render_body`), so the
            # field name is shown whole rather than clipped; the two-space indent nests it
            # visually under the "payload" heading above.
            rows.append((f"  {key}", Text(body, style="muted")))
        return rows
