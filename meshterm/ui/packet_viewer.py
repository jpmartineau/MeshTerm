# SPDX-License-Identifier: Apache-2.0
"""The packet viewer: one floating dialog that can open any packet that a list shows.

In each place where MeshTerm lists packets (the Live feed at this time, and any future
packet list), the rows share the chrome of this module for each kind: a colour and a
two-cell icon for each packet class. Each row can open into the same viewer: a centred
dialog over the list that shows the packet in full, with a layout for its kind:

* An advert shows the identity, type, and location of the node.
* Telemetry shows the node and the values that it reported.
* A packet from the RX log shows its parsed class and route. A flood also shows the
  region that it was scoped to, if any (:func:`~meshterm.ui.widgets.scope_text`). The
  packet also shows what it was addressed to: the recipient and the sender read from the
  packet body (refer to :mod:`~meshterm.core.frames`), or the token of a trace or an ack.
  It also shows the relay path that it came in on, in two forms. The first form is a
  ``via`` chain on the path line of the app (:mod:`~meshterm.ui.pathline`), which wraps
  at hop boundaries under its own lane. The second form, when the packet really went
  through a relay, is the route graph of the app (:mod:`~meshterm.ui.pathgraph`):
  origin → relays → us, the same layered picture that the Message paths dialog and the
  Trophy case draw. For an overheard channel-text packet that names a channel for which
  we hold the key, the viewer also shows the decrypted text (refer to
  :func:`~meshterm.core.channels.decrypt_channel_text`), although the device itself
  never decoded it for us.
* A message shows the sender, the conversation, and the text.
* An ack shows its code.

All kinds show these items: the class headline (icon + UPPERCASE class, the first row of
the card), the timestamp, and the node (under its shared node-type mark, with the name
coloured by the palette of the app, and the key in the key widget). They also show the
reception quality on the shared SNR bar, and each remaining raw field that the layout
of the kind does not already show.

When the viewer opens over a list, it pages through that list in place. ``↑``/``↓``
step to the newer/older packet, and Home/End jump to the newest/oldest. These are the
same keys that the list itself uses to walk its rows. Thus the user can read a burst
packet by packet, and does not have to go back out to the list. PgUp/PgDn scroll a tall
packet inside the dialog, as they do on all other screens. Esc closes the dialog.

Over a live list (a list that gives the viewer a ``source``), the top has **two stops
instead of one**, exactly as the top of the live feed itself. ``↑`` from the newest
packet, and Home from anywhere, go to the **pin**. On the pin, the viewer holds the top
position instead of a packet. Thus each new packet becomes the card that the user reads,
and the title says ``following`` instead of ``n/total``. ``↓`` steps off the pin onto
the packet that shows at that moment. That packet then moves down the list as newer
packets arrive. Thus a user who opened a packet to read it keeps it, and a user who
walked up to the top gets the stream. These are the same two stops, with the same keys,
as the feed under the dialog.
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
    short_frame,
)

#: Reads the scope of a packet from its raw payload (``ctx.region_store.scope_of`` in the
#: app). It gives ``None`` for a packet with no scope to state (a direct packet, or a route
#: type that was never kept).
ScopeOf = Callable[[dict | None], Scope | None]

#: Reads the scope of a decoded message entry from the flooded copies of it in the packet
#: log (refer to :func:`~meshterm.ui.livefeed_screen.message_scope_reader`).
MessageScopeOf = Callable[["PacketEntry"], Scope | None]


def unnamed_scope(raw: dict | None) -> Scope | None:
    """The scope of a packet, without region names: the fallback when no names are available.

    It still says all that the packet itself says: a plain flood is ``unscoped``, and a
    scoped flood is ``unknown scope <code>``. Only the names need the known regions (refer
    to :meth:`~meshterm.core.region_store.RegionStore.scope_of`). Thus a viewer opened
    without a store never draws less than the truth, only less than all of it.
    """
    return frame_scope(raw, ()) if isinstance(raw, dict) else None


#: The number of composed packet cards that the viewer caches (a few pages on each side
#: of the visible card).
_BODY_CACHE_MAX = 8

#: A friendly name for the parsed payload class of a raw packet (it mirrors the
#: ``PAYLOAD_TYPENAMES`` of the meshcore library), shown on the "class" row of a
#: ``packet`` entry.
_PAYLOAD_GLOSS = {
    "REQ": "request",
    "RESPONSE": "response",
    "TEXT_MSG": "dir messg",
    "ACK": "ack",
    "ADVERT": "advert",
    "GRP_TXT": "chan text",
    "GRP_DATA": "chan data",
    "ANON_REQ": "anon req",
    "PATH": "path",
    "TRACE": "trace",
    "MULTIPART": "multipart",
    "CONTROL": "control",
}

#: The raw-payload fields that a row of the kind layout already shows: the packet
#: structure parsed into "class"/"route", the channel crypto that
#: :meth:`PacketViewer._decrypt_rows` handles, or an advert field that duplicates
#: ``node``/``lat``/``lon`` or another field. The generic dump skips them, so that it does
#: not show them two times.
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

#: The style for each packet class. All packet lists and the viewer share it.
KIND_STYLES = {
    "advert": "accent",
    "telemetry": "brand",
    "packet": "muted",
    "message": "ok",
    "ack": "faint",
}

#: The two-cell icon for each packet class. Each icon has the width of an emoji, so on a
#: narrow terminal a list can replace the text label of the kind with only the icon, and
#: the lanes do not move. The fallback marks a class that is not in this table.
KIND_ICONS = {
    "advert": "📢",
    "telemetry": "📊",
    "packet": "📦",
    "message": "💬",
    "ack": "✅",
}
DEFAULT_ICON = "❔"

#: The two-cell icon for each parsed payload class of a raw ``packet`` entry. The first
#: class row of the viewer and the traffic meters of the dashboard share it. One glyph for
#: each concept: 📻 matches the Channels tool, 🎯 matches Trace, and a raw ADVERT/ACK
#: packet uses the icon of its kind again. :data:`DEFAULT_ICON` marks a typename that is
#: not in this table.
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

#: The colour of the route of a relayed packet on the route graph. It is the only path on
#: the canvas, so white reads as "the route that the packet took" (the single-walk
#: convention of the Trophy case).
_ROUTE_EDGE = (255, 255, 255)


@dataclass(slots=True)
class PacketEntry:
    """One listed packet, in the shape that each packet list stores and the viewer reads.

    On purpose, it is a flat union of what the three event families of the feed carry:
    the observations of the monitor, chat messages, and delivery acks. Thus one list type
    can hold all of them, and the viewer can select a layout by :attr:`kind`.

    Attributes:
        when: When the packet was heard.
        kind: The packet class (``advert``/``telemetry``/``packet``/``message``/``ack``).
        node: The stored hash of the node that transmitted the packet, when it is known.
        name: The name that the packet itself carried (the node name of an advert, the
            sender of a message), if any. If there is none, the display resolves
            :attr:`node`.
        snr: The reception SNR in dB, if measured (for ``packet`` rows: of the last
            relay).
        rssi: The reception strength in dBm, if measured.
        lat: The advertised latitude, when the packet shared a location.
        lon: The advertised longitude, when the packet shared a location.
        node_type: The advertised node type (refer to ``NODE_TYPE_*``), if the packet has
            one.
        path: The relay path of a ``packet`` row: comma-separated hop hashes in the order
            of propagation (empty means that it arrived direct, and ``None`` means that
            the class carries no path).
        where: The conversation of a message (``ch 3`` / ``direct``), or the code of an
            ack.
        channel: The slot index of a channel message. This is the fact behind the prose
            of ``where``. It is kept separate, so that a list can name the channel
            instead of showing its number.
        text: The body of a message, when it is known.
        raw: The raw event payload, for the values that a layout for one class cannot
            name.
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
        """Make an entry from an overheard observation (the event family of the monitor)."""
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

    On REGULAR, this function returns the emoji. On PICOCALC_LYRA,
    :func:`~meshterm.ui.theme.glyph` maps the emoji to a single-cell glyph from the
    512-glyph font.
    """
    emoji = KIND_ICONS.get(kind, DEFAULT_ICON)
    return glyph(emoji)


def payload_class(raw: dict | None) -> str | None:
    """The friendly payload-class label for a raw ``packet`` entry, or ``None`` if unknown.

    The function maps the ``payload_typename`` of the packet (``GRP_TXT``, ``TRACE``, …)
    through :data:`_PAYLOAD_GLOSS`. When a relayed flood names no origin node, this is the
    only identifying item that it carries. Thus a packet list can show "chan text" /
    "trace" instead of a bare ``?``.
    """
    if not isinstance(raw, dict):
        return None
    typename = raw.get("payload_typename")
    if not typename:
        return None
    return _PAYLOAD_GLOSS.get(typename, typename.lower())


def class_marks(entry: PacketEntry) -> tuple[str, str]:
    """What a packet really is: its icon and its plain-case class label.

    This is the only place where the app decides the class of a listed packet. Thus the
    headline of the viewer and the class lane of the feed can never disagree. The class
    of a raw ``packet`` entry is its parsed payload class (``chan text``, ``trace``, …),
    under its :data:`PAYLOAD_ICONS` glyph. The reason is that ``packet`` alone names only
    the event family in which the packet arrived, not the thing that it carries. A
    typename that is not in the table keeps the fallback mark and shows the raw typename.
    A packet with no class stays a plain ``packet``. Each other kind is its own class
    (``advert``, ``message``, …), under the shared :data:`KIND_ICONS` glyph. On
    PICOCALC_LYRA, the emoji are mapped to single glyphs.
    """
    if entry.kind == "packet":
        raw = entry.raw if isinstance(entry.raw, dict) else {}
        return payload_marks(raw.get("payload_typename"))
    return kind_icon(entry.kind), entry.kind


def payload_marks(typename: str | None) -> tuple[str, str]:
    """The icon and class label of a raw packet, from only its payload typename.

    This is the half of :func:`class_marks` for a ``packet`` entry. It is for a surface
    that holds a count for each typename instead of a packet (the traffic rows of the
    dashboard). Thus a class shows the same there as in the class lane of the live feed.
    ``None`` is the packet with no class.
    """
    if typename:
        emoji = PAYLOAD_ICONS.get(typename, DEFAULT_ICON)
        return glyph(emoji), _PAYLOAD_GLOSS.get(typename, typename.lower())
    return glyph(KIND_ICONS["packet"]), "packet"


def class_chrome(entry: PacketEntry) -> tuple[str, str]:
    """The class headline at the top of the card of the viewer: :func:`class_marks`, in capitals.

    The first row of the card is a headline, so it is in UPPERCASE. A list lane shows the
    same class in its plain case, directly from :func:`class_marks`.
    """
    icon, label = class_marks(entry)
    return icon, label.upper()


def node_label(
    entry: PacketEntry, resolve: NodeResolver, self_name: str | None = None
) -> tuple[str, str]:
    """The display name and style for the node of an entry, by the naming rule of the app.

    The name that ``resolve`` knows for the hash comes first (the contact list is the
    canonical identity). Then comes the name that the packet itself carried. A node that
    really has no name shows its hash. A name takes its stable palette hue
    (:func:`~meshterm.ui.theme.name_style`), but our node is always the pure-white
    ``you``. A bare hash stays muted, because colour is the signal for "this is a name".

    Args:
        entry: The packet with the node to label.
        resolve: Maps a node hash to a friendly name when the name is known.
        self_name: The name of our node, to find "us" (``None`` never matches).

    Returns:
        ``(label, style)``, ready to add to a row.
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
    """The reception row of the card (SNR with its quality bar, then RSSI), sized to its lane.

    The grid gets it with no size, as it gets the via path next to it. The width of the
    value column is known only after the width of the label lane is known (refer to
    :meth:`PacketViewer._label_width`), so the row makes its layout at render time. The two
    atoms are joined by the roomy separator of the app (:data:`~meshterm.ui.menus.SEP_ROOMY`).
    When the roomy form does not fit, they use the compact separator
    (:data:`~meshterm.ui.menus.SEP_COMPACT`). The header makes the same trade, wherever the
    cells are scarce. On the console of 53 cells they are scarce: a two-digit SNR next to a
    three-digit RSSI is too wide for the lane by exactly that padding. When the row wrapped,
    the bare word ``rssi`` was on a line of its own (JP, on the handheld, 2026-08-10).

    Attributes:
        snr: The signal-to-noise ratio in dB, if measured.
        rssi: The reception strength in dBm, if measured.
    """

    snr: float | None
    rssi: float | None

    def _line(self, separator: str) -> Text:
        """The row, drawn with ``separator`` between the two atoms."""
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
    """The floating packet dialog: one entry shown in full, with pages over its list.

    A read-only dialog. The opener gives it the list as rendered (newest first), and the
    index of the row that the user opened. Then ``↑``/``↓`` walk toward newer/older
    packets (the same keys that the list itself uses), Home/End jump to the ends,
    PgUp/PgDn scroll a tall body, and Esc closes. When the dialog opens over a single
    packet (a list with one entry), the paging keys do nothing.

    Over a live list (``source``), the top gets the second stop of the feed: the pin,
    where the viewer follows the stream instead of a packet (refer to the module docstring
    and :meth:`_pin`). Over a snapshot, there is nothing to follow, and ``↑`` on the
    newest packet clamps, as it always did.

    The dialog only grows (refer to :attr:`~meshterm.ui.tui.screen.Screen.grow_only`). A
    page from a short packet to a tall packet makes the box larger. But a page back keeps
    the box at that size (with blank padding under the shorter body), and does not centre
    a smaller box again. Thus the header that the user looks at never jumps while the
    user pages.
    """

    grow_only = True

    @property
    def picocalc_lyra_lane(self):
        """The shared pager over the body, with the jumps named for their destinations.

        Here, Home and End do not go to the ends of a body. They go to the ends of the
        list, the newest and the oldest packet. Thus the Shift companions say ``Newest``
        and ``Oldest``. The left-right order does not change: newest is the top of a
        newest-first list, so it keeps the right-hand slot behind Page ↑.

        The two banks are enabled by different conditions, because they move different
        things. The pager needs a body that is taller than the box. The jumps need a
        destination: a second packet, or, on a live list, the pin. ``Newest`` gets to the
        pin also when the list holds one packet (as the ``Top`` chip of the feed does, on
        the screen under this dialog). When the dialog opens over a single packet of a
        snapshot, all four chips are dim at the same time.
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
        message_scope: MessageScopeOf | None = None,
    ) -> None:
        """Open the viewer over a packet list.

        Args:
            entries: The packets of the list, newest first: the snapshot at the open.
            index: The entry to open on.
            resolve: Maps a node hash to a friendly name when the name is known.
            prefix_bytes: The path-hash width to light in the shown hashes (0 is none).
            self_name: The name of our node, drawn white wherever it occurs.
            on_navigate: Called with the newly shown entry each time a page key moves the
                viewer. Thus the list can move its own highlight at the same time.
            on_pin: Called when the viewer moves onto the pin. Thus a list that has the
                same stop (the live feed) can also follow the stream, and still follows it
                when the dialog closes. It is never called without a ``source``, because
                the pin is a stop only where the list is live.
            channels: The configured channels of the device, as ``(name, secret)``
                pairs. The viewer tries them on the channel-text packet of an overheard
                ``packet`` entry (refer to
                :func:`~meshterm.core.channels.decrypt_channel_text`). Thus a raw packet
                that the device never decoded for us can still be read when we hold the
                key.
            source: An optional callable with no arguments that returns the current
                packets of the list (newest first). When it is given, the viewer reads it
                again at each paint and key press, and finds the shown packet again by
                identity. Thus the packets that arrive while the dialog is open become
                available (``↑`` walks up into them), and the viewer does not stay frozen
                at its first snapshot.
            type_of: Maps a relay hash to its node type, so that the route graph of a
                relayed packet marks a repeater ``▲`` (and other types) instead of a
                generic dot. ``None`` keeps the plain fallback (a dot for a named node, a
                ring for an unknown node).
            key_of: Maps the display name of an origin back to the key of its node, so
                that the left endpoint of the route graph gets its key-derived hue.
                ``None`` (or a name that does not resolve) leaves it muted.
            scope_of: Reads the scope of a flood from its raw packet, against the regions
                known by name (``ctx.region_store.scope_of``). Thus the ``scope`` row can
                say into which region a scoped flood was sent. ``None`` falls back to
                :func:`unnamed_scope`: the viewer still tells a scoped flood from an
                unscoped flood, and a scoped flood shows its code in the place of a name.
            message_scope: Reads the scope of a decoded ``message`` entry from the
                flooded copies of it in the packet log, because its own raw payload
                cannot carry the scope (refer to
                :func:`~meshterm.ui.livefeed_screen.message_scope_reader`). With ``None``,
                the card of a message has no ``scope`` row.
        """
        super().__init__()
        self._entries = list(entries)
        self._index = max(0, min(index, len(self._entries) - 1))
        # The composed cards for each (entry, width, age-minute). Refer to render_body.
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
        self._message_scope = message_scope
        #: The packet that shows now, followed by identity. Thus a live prepend to the
        #: source (which moves each index) never moves the viewer onto another packet.
        self._current: PacketEntry | None = self._entries[self._index] if self._entries else None
        #: Whether the viewer is on the pin: the stop above the newest packet, which
        #: holds the top position instead of a packet. It means that ``_index == 0``. The
        #: pin is a mode on the newest entry, so all the code that reads the viewer (the
        #: card, the scroll, the cache) needs no special case. Only the title and the
        #: moves are different. An open never goes to the pin, because Enter selected
        #: this packet (refer to
        #: :meth:`~meshterm.ui.livefeed_screen.LiveFeedScreen._open_packet`). Thus the
        #: user walks up to the stream on purpose, or not at all.
        self._pinned = False
        self._update_footer()
        self._set_title()

    def _update_footer(self) -> None:
        """Set the footer hint to the moves that are available now.

        There are three shapes, because the top of a live list has two stops. On the pin,
        only ``↓`` moves, and the atom says what a step off the pin gives: the visible
        packet, held against the next arrival. One stop below, ``↑`` and ``↓`` do
        different things (one follows the stream, the other walks to older packets). Thus
        each gets its own atom, and they do not share ``newer/older``, which names
        neither. In all other places, the pair walks the list and reads as one atom. A
        single packet with nothing to follow keeps the bare ``Esc close`` that it always
        had.
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
        """Whether this viewer has a stream to follow, which is what a ``source`` is.

        Over a snapshot, there is no second stop above the newest packet. Nothing will
        ever arrive there, so the pin is an empty mode, and ``↑`` clamps instead.
        """
        return self._source is not None

    def _sync(self) -> None:
        """Refresh the entries from the live source, and find the shown packet again by identity.

        Without a ``source``, this method does nothing. Otherwise, it reads the list again
        (with the packets that arrived after the last paint), and the viewer stays on the
        same packet object. Only the index of that packet changes, when newer packets are
        added before it. Thus the page keys measure against the current list, and the
        newly arrived packets are available above the packet that the user reads. If the
        packet left the list because of its age, the viewer goes to the nearest index
        that remains.

        The pin is different: it is attached to the position. Thus, after a new read, the
        pin is on the newest packet, and the card under it changes. This method runs at
        each paint and before each move, so to follow costs nothing more than the paint
        that the feed already does.
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
        # No emoji in a dialog title (a rule of the standards). The class row has the icon.
        # The position atom says which of the two things the viewer does: a count while it
        # holds a packet, and ``following`` while it holds the top of the stream. The word
        # replaces a permanent ``1/n``, because under the pin the card changes, not the
        # number.
        entry = self._entries[self._index]
        title = entry.kind
        if self._pinned:
            title += " · following"
        elif len(self._entries) > 1:
            title += f" · {self._index + 1}/{len(self._entries)}"
        self.title = title

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Page through the list, follow the stream, scroll a tall entry, or close."""
        if action in ("up", "down", "home", "ctrl_home", "end", "ctrl_end"):
            # Read the live list again first, so that the newly arrived packets are in
            # range before the step. If not, the first ``↑`` from the newest packet can
            # never get to them.
            self._sync()
        if action == "up":
            # From the newest packet, ``↑`` has one more destination on a live list: the
            # pin. On a snapshot, the same press clamps, because there is no stream to
            # follow.
            if self._index == 0:
                self._pin()
            else:
                self._jump(self._index - 1)
        elif action == "down":
            # From the pin, ``↓`` does not move down a row. It goes to the same packet that
            # shows under the pin, and holds it now. This is exactly the ``↓`` of the feed
            # from its own pin.
            self._jump(self._index if self._pinned else self._index + 1)
        elif action in ("home", "ctrl_home"):
            # "Take me back to the top" continues the stream where there is one. It does
            # not stop on the packet that is newest at this instant. This is the Home of
            # the feed again, and the destination of the ``Newest`` chip on the console.
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
        """Hold the entry at ``index`` (clamped), and leave the pin.

        To go to an entry is, by definition, to hold a packet instead of the stream. Thus
        this is also the way off the pin. It is also the only move that does something
        while the index does not change, which is exactly what ``↓`` from the pin is. The
        scroll resets only where the packet itself changes (refer to :meth:`_show`). Thus
        that press keeps the user at the same position in the card that they already
        read.
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

        Over a snapshot (nothing to follow), and when the viewer is already on the pin,
        this method does nothing. Otherwise, it reads the list again, so that the pin goes
        to the newest packet at this time. Then it tells the opener. Thus a list that has
        the same stop also follows the stream, and still follows it when the dialog closes
        and the list shows again.
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
        """Put the viewer on entry ``index``, and show a different packet from its top.

        The scroll is part of the packet, not of the position. A page onto another card
        starts that card at its start. But a return to the card that already shows (``↓``
        from the pin) keeps the user exactly at the scroll position that they had.
        """
        self._index = index
        entry = self._entries[index]
        if entry is not self._current:
            self._current = entry
            self.scroll = 0

    # --- rendering -------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Show the current entry as labelled rows, with a layout for its kind.

        For a relayed ``packet``, the route graph of the app (:mod:`~meshterm.ui.pathgraph`)
        goes between its parsed detail (heard/from/class/via) and its extras
        (location/text/raw). The via row names each hop exactly, and the graph shows the
        same relay chain as a shape: the origin at the left, us at the right. The two
        label/value grids around the graph share one label-lane width, so that their rows
        still align across the picture.
        """
        # Add the packets that arrived after the last paint, and keep the viewer on the
        # same packet. Thus the ``n/total`` of the title and the available range stay
        # current.
        self._sync()
        entry = self._entries[self._index]
        # An entry does not change after its capture. Thus its full card (the rows, the
        # decrypted payload, the route-graph layout) is a pure function of the entry, the
        # width, and the minute that its "heard" age shows (the only row that changes
        # with time). The cache key is exactly that: the paint tick makes the layout of
        # the graph again only when a minute boundary changes the visible text.
        age_minute = int(max(0.0, (utcnow() - entry.when).total_seconds()) // 60)
        # The card is also a function of the scope of the flood, which is not fixed at
        # capture. A region that MeshTerm learns while the card is open (the list of a
        # repeater arrives, or the user types a name) names a code that was unknown a
        # moment before. The store caches the lookup, so a request at each paint costs a
        # dict hit.
        # The card is also a function of whether the frame is short, because a short
        # frame removes the node-type legend.
        short = short_frame(self._scroll_viewport)
        key = (id(entry), width, age_minute, self._entry_scope(entry), short)
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
            if not short:  # no legend on a short frame (refer to widgets.short_frame)
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
        """The shared label-lane width, so that wrapped values hang under their own block.

        One fixed width for all the grids on the card (the hanging-indent rule of the app),
        never column zero. The width is that of the widest label (also the full name of a
        raw payload field, so that no label is clipped), plus a gutter of one cell. But it
        is never so wide that the value column has less than 20 cells. (A very long key
        then gets an ellipsis, and does not make the value column too narrow.)
        """
        widest = max((len(label) for label, _ in rows), default=0)
        return max(10, min(widest + 1, width - 20))

    @staticmethod
    def _grid_lines(rows: list[tuple[str, RenderableType]], width: int, label_w: int) -> list[str]:
        """Render one label/value grid to ANSI lines (no rows give no lines)."""
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

        The class headline, the common core, then the parsed fields of a packet.
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
        elif entry.kind == "message":
            # A decoded message has no route data of its own. Its scope is read from the
            # flooded copies of it in the packet log (refer to :meth:`_entry_scope`).
            scope = self._entry_scope(entry)
            if scope is not None:
                rows.append(("scope", self._scope_text(scope)))
        return rows

    def _node_marker(self, entry: PacketEntry, style: str) -> tuple[str, str]:
        """The node-type mark at the start of the ``from`` row: the shared node glyphs of the app.

        These are the same marks that the map, the contact list, and the route graph below
        put on a node (``★`` us, ``▲`` repeater, ``■`` room, ``◉`` sensor, ``●`` node).
        Thus the sender of a packet reads as the same kind of thing here as everywhere
        else. The ``type`` row below gives in words what the glyph shows quickly. The type
        comes from the packet itself when the packet carried one (an advert does). Else it
        comes from what the contacts know about its hash. When neither can give a type,
        the node keeps the ``○`` unknown ring, and the viewer does not show it as a plain
        client.

        Args:
            entry: The packet with the sender to mark.
            style: The style that :func:`node_label` gave the name (``"you"`` marks us).

        Returns:
            The ``(glyph, style)`` pair, ready to add to the row.
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
        """The rows below the route graph: the location, the message/ack fields, the raw dump."""
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
        """Draw the origin → relays → us route of a relayed packet on the route graph of the app.

        Only a ``packet`` entry that really went through a relay draws a graph. For a
        direct arrival, the ``via`` row (``direct — no relays``) already says all that
        there is to say, and a two-node graph wastes the height. The relay chain is the
        path of the packet (the hop nearest to the originator first). The left endpoint is
        the origin, when the packet named one (the ``adv_key`` of an advert). The other
        classes arrive with no origin, drawn ``?``. The right endpoint is always us. This
        is the same layered route-graph widget that the Message paths dialog and the
        Trophy case draw, with a single white path.

        The graph is drawn with ``allow_duplicate_nodes``, because this chain is observed.
        We did not compose it, a one-byte hash names each of its hops, and a repeated hop
        is as probably two nodes with the same hash as a real loop. If such a repeat goes
        into one marker, it makes a cycle that the left-to-right flow cannot place: the
        walk becomes a pile of markers one column wide. Thus each visit gets its own
        marker, and the chain draws in the order in which it was heard. The
        :func:`~meshterm.ui.widgets.revisit_note` of the ``via`` row says why a name
        occurs two times.
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
        """The name of the origin for the left endpoint of the graph, or ``None`` if unknown.

        This is the naming rule of the ``from`` row (a resolved contact first, then the
        name that the packet carried). But an origin with no name also returns ``None``.
        Thus the graph draws a plain ``?`` endpoint, and does not show a bare hash as if it
        were a named node.
        """
        if entry.node:
            resolved = self._resolve(entry.node)
            if resolved and resolved != entry.node:
                return resolved
        return entry.name or None

    @staticmethod
    def _reception(entry: PacketEntry) -> _Reception:
        """SNR (with the shared quality bar) and RSSI on one line. Refer to :class:`_Reception`."""
        return _Reception(entry.snr, entry.rssi)

    def _via_path(self, entry: PacketEntry) -> PathLine:
        """The relay chain of a ``packet`` entry as a path line (:mod:`~meshterm.ui.pathline`).

        The grid gets it with no size. The line renders itself into the cell width that
        the value lane gives, wraps at hop boundaries (never in a name, never in a chip),
        and hangs its continuation lines under the value block. The dialog has rows to
        use, so a long chain shows in full here. The feed row behind the dialog does not
        show the relay path at all.

        A trace with no hops is the only case where an empty chain does not mean a direct
        packet. A trace never names its relays. It stores a reading for each hop instead
        (refer to :func:`~meshterm.core.frames.trace_link_snrs`). Thus a walked trace
        arrives with an empty path and a full set of readings, and the text
        ``direct — no relays`` clearly contradicts the ``links`` row two lines above.
        Instead, the row says how far the packet got, which is the only thing that those
        readings do prove.

        The chain is the middle of the route. The ``path`` field names the relays that
        forwarded the packet, and neither the node that sent it nor the node that received
        it. Thus both ends are drawn open (``from_origin``/``to_destination``). The chevron
        says that the route continues past the chip. A flat end claims that the first relay
        was the origin.
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
        """What an empty relay chain means for the class of this packet."""
        readings = (entry.raw or {}).get("trace_snrs") if isinstance(entry.raw, dict) else None
        if readings:
            hops = len(readings)
            return (
                f"{hops} hop{'' if hops == 1 else 's'} walked "
                "— a trace logs each leg, not its relays"
            )
        return "direct — no relays"

    def _packet_rows(self, entry: PacketEntry) -> list[tuple[str, RenderableType]]:
        """What a raw ``packet`` entry has to say about itself.

        Its addressing, its parsed class and route, and its relay path. For a channel
        packet that names a channel for which we hold the key, also its channel and its
        plaintext.
        """
        raw = entry.raw if isinstance(entry.raw, dict) else {}
        rows: list[tuple[str, RenderableType]] = []
        typename = raw.get("payload_typename")  # the class is at the top (class_chrome)
        rows.extend(self._addressing_rows(raw))
        route = raw.get("route_typename")
        if route:
            scope = self._entry_scope(entry)
            rows.append(("route", self._route_text(route, scope)))
            if scope is not None:
                rows.append(("scope", self._scope_text(scope)))
        rows.append(("via", self._via_path(entry)))
        # The number that the chain above encodes but never states, under the chain, as
        # the hop atom of the app. This is the same atom that the routes of the node page
        # and the trace scenarios carry (:func:`~meshterm.ui.pathline.hops_atom`), so a
        # count reads the same wherever a path has one. Only where there is a chain to
        # count: the row above already says an empty ``path`` in words
        # (:meth:`_empty_via`), and a second line ``direct`` under ``direct — no relays``
        # gives the same answer two times.
        relays = [hop for hop in (entry.path or "").split(",") if hop]
        if relays:
            rows.append(("", hops_atom(len(relays))))
        # A chain that names one hop two times looks like an error until it is explained.
        # The graph below also draws that hop two times (refer to ``_graph_lines``), so the
        # note is for both.
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
            # The body of a datagram is not text, so there is nothing to decode for the
            # user. But the MAC that authorises a decrypt also names its channel.
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
        """The scope of the packet of an entry, or ``None`` where it has no scope to state.

        A raw ``packet`` entry carries the route type and the transport codes, from which
        the scope is read. A decoded ``message`` carries neither, because the device gives
        the text, not the packet. Thus its scope is read from the flooded copies of it in
        the packet log, where the caller gave a ``message_scope`` function (the live feed
        gives one for channel messages). Each other kind answers ``None`` and draws
        nothing.
        """
        if entry.kind == "message" and self._message_scope is not None:
            return self._message_scope(entry)
        if entry.kind != "packet" or not isinstance(entry.raw, dict):
            return None
        return self._scope_of(entry.raw)

    @staticmethod
    def _route_text(route: str, scope: Scope | None) -> Text:
        """The ``route`` row: how the packet was routed, ``flood`` or ``direct``.

        A scoped flood shows ``flood``, not the ``tc flood`` of the library. That name was
        for the wire mechanism (transport codes), and the user had to know that it meant
        scoped. A scoped flood and an unscoped flood have the same routing. They differ in
        the region, which has its own row below (:meth:`_scope_text`).

        A direct packet keeps only its route word. The codes of a ``TC_DIRECT`` packet are
        the "to nowhere" pair that a contact share uses, not a region, so it has no scope
        row.
        """
        if scope is None:
            return Text(route.replace("_", " ").lower())
        return Text("flood")

    @staticmethod
    def _scope_text(scope: Scope) -> Text:
        """The ``scope`` row: the region into which a flood was sent, on its own line.

        It is a separate row with a label, not an atom after the route. When the user
        opens the card from the SCOPE lane of the feed, the scope is what they want to
        see. A card has the space to say it in full, where the lane had ten cells. The
        label says "scope", so the value is only the region (``harbour``, in the ``scope``
        style). When no known region name gives the code, the row shows
        ``unknown region · code 3fa1``. The code stays, so that the user can see when two
        packets share it. A plain flood shows ``unscoped``.
        """
        if scope.state == "scoped" and scope.region:
            return scope_text(scope, bare=True)
        if scope.scoped:
            text = Text("unknown region", style="muted")
            if scope.code:
                text.append(f" · code {scope.code}", style="muted")
            return text
        return Text("unscoped", style="muted")

    def _addressing_rows(self, raw: dict) -> list[tuple[str, RenderableType]]:
        """For whom a packet was, from whom it says it was, and the token that it carries.

        These are the fields that :mod:`~meshterm.core.frames` reads from the packet body.
        They are rows of the card, and not left to the raw dump at the bottom: a recipient
        is a node, and a node must show with a resolved name and the hash widget of the
        app, not as a bare hex value in a debug list. One byte of its key names each
        endpoint, so the name is a good guess, the same as the name of a relay hop (and the
        lit hash next to it shows exactly which byte matched). The sender of an anonymous
        request carries its full key, which resolves fully.

        Args:
            raw: The raw payload of the packet.

        Returns:
            The rows to put before the route/via block (empty for a class that addresses
            nothing).
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
            # An ack identifies the message that it answers, by the checksum of that message.
            rows.append(("acks", Text(raw["ack_crc"], style="muted")))
        return rows

    def _link_readings(self, readings: list[float]) -> Text:
        """How well each leg of the walk of a trace was heard, in the order of the walk.

        A trace is the only class that reports on the mesh while it goes through it. Each
        node that forwards it records the SNR at which it heard the node before it. Thus
        an overheard trace carries a reading for each hop that it went through, and the
        number of readings shows how far along its route the packet was when we heard it.
        The readings go from the oldest hop first, in the same direction as a path line.
        :func:`~meshterm.ui.theme.snr_style` colours each reading, as it colours all other
        reception values in the app.

        Args:
            readings: The SNR of each hop in dB, in the order of the walk.

        Returns:
            The readings as one line, with separators.
        """
        line = Text()
        for at, value in enumerate(readings):
            if at:
                line.append(SEP_COMPACT, style="muted")
            line.append(f"{value:+.2f} dB", style=snr_style(value))
        return line

    def _endpoint(self, value: str) -> Text:
        """One end of an addressed packet: its resolved name, then the key that named it.

        This is the presentation of the ``from`` row, without the node-type glyph: a
        one-byte hash is too weak an identity for a type mark. A node that we cannot name
        shows only the hash, all grey (``known=False``), as each unknown-node hash in the
        app does.
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
        """Try the key of each known channel on an overheard channel-text packet.

        The packet names its channel only by a one-byte hash fingerprint (several channels
        can have the same fingerprint). Thus
        :func:`~meshterm.core.channels.decrypt_channel_text` confirms the match by MAC
        before it uses a key to decrypt anything. When we do not hold the key for a
        channel, or cannot confirm a fingerprint, the card reports that, and does not
        silently leave the rows out.
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
        """The remaining raw payload fields, one labelled row each (the main content of telemetry).

        The method skips the fields that the layout of the kind already shows. The other
        fields render as flat ``key value`` rows. Thus the channels of a telemetry packet
        (or a new firmware field that the layout does not know yet) are still visible
        without a debugger.
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
            # The label lane becomes wider to fit the full key (refer to :meth:`render_body`).
            # Thus the field name shows in full, and is not clipped. The two-space indent
            # puts it visually under the "payload" heading above.
            rows.append((f"  {key}", Text(body, style="muted")))
        return rows
