# SPDX-License-Identifier: Apache-2.0
"""The Live feed: each packet when it arrives, full-screen, with the newest first.

This screen is the interactive face of the ``livefeed`` tool. It was the feed panel of
the dashboard, and now it is a screen of its own. One list, which MeshTerm paints again
continuously, shows the latest packets with the newest first: the time, the class (its
name, next to an icon where the platform draws icons), the subject, the SNR/RSSI, and the
conversation of a message.

A row intentionally does not show the relay path on which its packet arrived. A route is
a shape, not a lane. When a row put a route into the cells that remained at the right
edge, the result was always a stub. Thus the route belongs to the packet viewer, which
has the space to draw it as a wrapped path line over its graph. The feed tells what
arrived and how well it was heard. Enter tells how it arrived.

A **scope** lane is between the subject and the readings, at each width. It shows:

- the region into which a flood was sent (``harbour``),
- ``? 3fa1`` for a region that has no name here,
- a muted ``-`` for a plain flood, and
- nothing for a direct packet, because no repeater filters a direct packet by region.

The scope lane never collapses, and no other lane collapses. On a narrow terminal, the
user scrolls the highlighted row sideways instead (refer to :func:`_lanes_for`).

The content of the subject lane changes with the class of the packet: the lane shows
what the packet is about (refer to :meth:`LiveFeedScreen._feed_subject`). An advert or a
telemetry packet is about the node that sent it, so the lane names the node. But a
channel text is about its channel. A direct message or a request is about the two nodes
between which it travels. A trace is about its tag. Only a class that says nothing about
itself leaves the lane empty.

The screen starts with rows from the stored history, so it is full when it opens. Then
it gets new rows live from the event hub. ``↑``/``↓`` move through the rows, and
PgUp/PgDn/Home/End page and jump through them. On a terminal that is too narrow for all
of a row, ``←``/``→`` scroll the highlighted row sideways.

The top of the list has **two stops, not one**. The two stops are the difference between
a user who watches the feed and a user who reads it:

- The first stop is the *pin*. It holds the topmost position, so each packet that
  arrives takes the highlight with it. The pointer shows as ``^`` (it points above the
  top of the list, at the traffic that is still to come), instead of the usual ``❯`` of
  the app.
- One ``↓`` from the pin does not move down a row. It goes to the same newest packet,
  which is now selected in the usual way, under a plain ``❯``. From there, the highlight
  belongs to that packet, and it moves down when newer packets push it.

Thus the pin follows the stream, and a selection follows a packet. The mark in the
pointer lane shows which of the two the user does. The screen opens on the pin, because
the usual state of a live feed is that the user watches it. ``↑`` from the newest packet
goes back to the pin, and Home goes directly back to the pin from any position.

Enter opens the highlighted packet in the shared
:class:`~meshterm.ui.packet_viewer.PacketViewer`. The viewer then pages through the feed
itself with the same ``↑``/``↓``. If an overheard channel-text packet names a channel for
which we have the key, the viewer decrypts it. **The viewer has the two stops too.** It
holds the same pin above the newest packet, so ``↑`` from the top follows the stream
there too (each packet that arrives becomes the card that the user reads). The viewer
also moves the cursor of the feed with it, the pin included. Thus, when the dialog
closes, the feed is on the stop that the user had in the dialog, not on the stop that
the user left when the dialog opened.

The screen has no subscriptions of its own. The opener (:func:`open_livefeed`) connects
the hub subscription and the paint that occurs one time each second, and removes them
when the screen resolves. The newest packet is highlighted when the screen opens. Esc
leaves the screen directly.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

from rich.text import Text

from ..core.channels import identify_channel, split_channel_sender
from ..core.events import EventKind, MeshEvent
from ..core.frames import CHANNEL_CLASSES, ENDPOINT_HASH_BYTES
from ..core.models import Observation, utcnow
from ..platforms import Platform, on_platform
from .menus import fit_cells
from .packet_viewer import (
    KIND_STYLES,
    MessageScopeOf,
    PacketEntry,
    PacketViewer,
    ScopeOf,
    class_marks,
    node_label,
    unnamed_scope,
)
from .pathline import SELF_GLYPH, PathHop, PathLine
from .theme import name_style, snr_style
from .tui.render import crop_cells, render_to_ansi
from .tui.screen import ListWindow, Screen
from .widgets import NameKeyResolver, TypeOf, scope_text

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.regions import Scope

#: The seconds between full paints while the feed is open. These paints keep the chrome
#: that shows ages correct, also between hub events.
_REFRESH_S = 1.0

#: The number of feed rows that the screen keeps. The feed is a list window in the
#: screen, so this number is the depth of the history, not the layout. It is the only
#: limit of the feed, so it is the one number that sets how far back the screen remembers.
#: A busy hour on a real mesh has some hundreds of packets. Thus a limit of a hundred rows
#: forgot the last ten minutes, at the time when the feed was most useful. Here, more
#: depth costs almost nothing, because the paint uses a list window
#: (:meth:`LiveFeedScreen._feed_lines` formats only the visible rows). The limit costs one
#: pointer for each entry and a few milliseconds to build the entries when the screen
#: opens, and nothing for each paint.
_FEED_CAP = 500

#: How far back the opening seed can go: **no limit**. Only the cap sets how much history
#: the feed holds, and a clock must not overrule it. With a time limit, a quiet mesh opens
#: a half-empty screen. The cause is not that nothing was ever heard, but that nothing was
#: heard recently, and the user can already see that.
#:
#: The mechanism did not change. :meth:`~meshterm.persistence.repository.Repository
#: .recent_observations` still takes a ``since``, and this value is a floor that is
#: earlier than all stored rows. The query stays fast, because ``observed_at`` has an
#: index: the query goes backwards through the index and stops after :data:`_FEED_CAP`
#: rows. Thus a wider time window reads no more rows than a narrower one did. This value
#: is intentionally not :data:`~meshterm.persistence.repository.OBSERVATION_WINDOW`, which
#: the feed used before. That value is the limit of the rolling statistics of the
#: dashboard, and it is what the RF health card of the dashboard means by "reception over
#: 2 h".
_FEED_SEED_FLOOR = datetime.min.replace(tzinfo=timezone.utc)

#: The fixed width of the subject lane of the feed. A longer subject ellipsizes, so that
#: the columns stay aligned. The width is sufficient for a node name, which is what most
#: classes put here. It is also sufficient for the two endpoints of an addressed packet to
#: show as ``a1 → Hilltop-Repe…``, instead of only bare hashes. The lanes were sized for a
#: 72-cell screen, and a wider subject lane pushes the row further past it. Thus the cells
#: that the lane cannot hold are for the viewer to use.
_FEED_SUBJECT_WIDTH = 18

#: The fixed width of the class lane. It is sufficient for the longest class of a packet
#: in the app (``telemetry``, ``chan text``, ``multipart``), so each label shows fully.
#: JP made the names short intentionally on 2026-09-25 (``dir messg``, ``anon req``). The
#: lane never collapses, so each cell that it uses pushes the readings of the row one more
#: cell off the screen.
_FEED_CLASS_WIDTH = 9

# The remaining lanes, as the row and its column header both measure them. There is one
# set of numbers, so a header label always stays over the values that it names, and a
# lane that a packet left empty pads to the same width as a full lane.
#: The cells between two lanes that are next to each other.
_LANE_GAP = 2
#: ``HH:MM:SS``, plus the gap after it.
_TIME_LANE = 8 + _LANE_GAP
#: The class icon and the space after it, or nothing where the platform draws no icons
#: (:func:`_bind_class_icon` sets this value).
_ICON_LANE = 3


@on_platform
def _bind_class_icon(platform: Platform) -> None:
    """Bind the lane of the class icon to the platform (this runs now and at each switch).

    The class name is always drawn (it never collapses: JP decided this on 2026-09-25).
    Thus the icon next to it is only a cue for the family, not the information itself.
    That is what :attr:`~meshterm.platforms.Platform.menu_icons` controls. On the
    PicoCalc, a class icon can only be a one-glyph substitute, so the lane is removed and
    the row gets its three cells.
    """
    global _ICON_LANE
    _ICON_LANE = 3 if platform.menu_icons else 0


#: The cells in which an SNR right-aligns its number: ``+13.2``, ``-20.5`` (a sign, two
#: digits, and a tenth).
_SNR_W = 5
#: The cells in which an RSSI right-aligns its number: ``-120`` at its weakest, in whole
#: dBm. The width comes from the reading, not from the field of the SNR. When the RSSI
#: used the SNR field, the extra cell of that field made a gap between the two readings
#: that was two times as wide as each other gap on the row.
_RSSI_W = 4
#: ``+13.2 dB``: the number field, then its unit.
_SNR_LANE = _SNR_W + len(" dB")
#: ``  -105 dBm``: the lane gap, the number field, then its unit.
_RSSI_LANE = _LANE_GAP + _RSSI_W + len(" dBm")

#: The width of the scope lane. It holds all of ``? 3fa1`` (an unnamed scope) and all of
#: a short region name. A longer name ellipsizes. The full name is on the ``scope`` row of
#: the viewer, one key press away, the same as all other text that a lane cuts.
_FEED_SCOPE_WIDTH = 10

#: The scope lane, with the gap that separates it from the readings after it.
_SCOPE_LANE = _FEED_SCOPE_WIDTH + _LANE_GAP


def _feed_scope(scope: Scope | None) -> Text:
    """One row's scope cell: the region, ``? 3fa1`` if unnamed, or a muted dash if unscoped.

    The cell is blank for a direct packet (or for a row that is not a packet), because it
    has no scope. A dash there says that the packet is a plain flood, which is not true.
    """
    if scope is not None and not scope.scoped:
        return Text("-", style="muted")
    return scope_text(scope, bare=True)


class _Lanes(NamedTuple):
    """The lane widths in which one paint lays out its rows.

    Attributes:
        subject: The width of the subject lane (:data:`_FEED_SUBJECT_WIDTH`).
    """

    subject: int


def _lanes_for(width: int) -> _Lanes:
    """The lanes that a row has at ``width``: all of them, at each width.

    No lane collapses (JP, 2026-09-25). Before, the class name and then the scope were
    removed on a narrow terminal. Each time, the removed lane was one that the user opened
    the feed to see. Thus the user reads a row that is wider than the screen in the same
    way as in all of the app: the highlighted row moves sideways with ``←→``. On a
    72-cell terminal, this scroll gets to the readings at the end of the row. On the 53
    cells of the PicoCalc, it gets to all of the row after the subject.
    """
    return _Lanes(_FEED_SUBJECT_WIDTH)


#: The cells by which one press of ←/→ moves the highlighted row. This is the step of the
#: select list of the app (:attr:`~meshterm.ui.tui.select.SelectScreen._HSCROLL_STEP`),
#: so a row here scrolls at the same rate as a row in all other places.
_HSCROLL_STEP = 8

#: The moves that cancel the horizontal scroll of the highlighted row. Each row scrolls
#: independently, as the rows of an ``hscroll`` select list do. When the highlight goes
#: to a new packet, that row always starts at its own beginning.
_HSHIFT_RESET = frozenset(
    {
        "up",
        "down",
        "pageup",
        "pagedown",
        "space",
        "home",
        "ctrl_home",
        "end",
        "ctrl_end",
    }
)

#: The pointer on a row that is selected in the usual way. It is the select-list pointer
#: of the app, and it points into the list, at the one row that it marks.
_CURSOR = "❯ "

#: The pointer on the pinned newest row (refer to the module docstring). It is in the same
#: lane, but it points above the top of the list, at the traffic that is still to arrive.
#: The reason is that the pin is attached to that traffic: not to this packet, but to the
#: packet that is the newest at each time. It is intentionally not ``↑``, because this
#: screen already uses ``↑`` for the ``↑ n more`` line of the list window. Here, ``↑``
#: means "there is more above", and the top of the feed can never mean that. It is
#: also not ``▲``, which is the repeater in the node-type set of the app. No other part of
#: the app uses a caret, a caret means "topmost" on both platforms, and it is plain ASCII.
#: Thus it is not necessary to add it to the glyph map of the console font (refer to
#: :mod:`meshterm.ui.fontset`).
_PIN_CURSOR = "^ "


def _channel_sender(text: str | None) -> str | None:
    """The sender in the ``Name: `` prefix of a channel message, or ``None`` if absent.

    This function uses the shared parse of the protocol layer (refer to
    :func:`~meshterm.core.channels.split_channel_sender`). Thus the subject lane of the
    feed names the same sender as the chat transcript.
    """
    name, _body = split_channel_sender(text or "")
    return name


class LiveFeedScreen(Screen):
    """The full-screen packet stream. It renders the state, and the opener gives it data."""

    floating = False

    @property
    def picocalc_lyra_lane(self):
        """The shared lane, which stays dim until there are packets to move through.

        Here, each navigation key moves the cursor over the feed, not a scroll offset. But
        the words are the same in the two cases, because the list window follows the
        highlight. Thus the shared labels stay. ``Top`` and ``Bottom`` keep their literal
        meaning intentionally: the top of this list is the pin on the newest packet, and
        its bottom is the oldest packet, where End goes. Where there is no hint line to
        name the pin, ``Top`` is how the user gets to it. Thus, with one chip, a feed that
        the user read down into the history follows the stream again.

        An empty feed has no cursor and no place to move to. Thus all four chips are dim
        until the first packet arrives.
        """
        from .tui.fkeys import default_lane

        return default_lane(nav=bool(self._feed))

    def __init__(
        self,
        *,
        session: Any,
        resolve: Any,
        seed: list[Observation],
        prefix_bytes: int = 0,
        self_name: str | None = None,
        channels: Sequence[tuple[str, bytes]] = (),
        channel_names: Mapping[int, str] | None = None,
        type_of: TypeOf | None = None,
        key_of: NameKeyResolver | None = None,
        scope_of: ScopeOf | None = None,
        message_scope: MessageScopeOf | None = None,
    ) -> None:
        """Create the feed screen over its data sources.

        Args:
            session: The running TUI session (for paints and the packet viewer).
            resolve: Gives the contact name for a node hash, when the name is known.
            seed: The stored observations to open with (the oldest first, as the
                repository returns them). The newest :data:`_FEED_CAP` observations become
                the opening feed.
            prefix_bytes: The width of the hash to light in the keys of the packet viewer.
            self_name: The name of our node, drawn in white where it appears.
            channels: The configured channels of the device, as ``(name, secret)`` pairs.
                For an overheard channel packet, the subject lane names the channel whose
                key gives the same MAC as the packet. Each
                :class:`~meshterm.ui.packet_viewer.PacketViewer` that opens uses these keys
                to decrypt.
            channel_names: The channel names of the device, by slot index. Thus a channel
                message that the device decoded for us shows under the same channel name
                as an overheard packet on that channel, instead of under its slot number.
            type_of: Gives the node type for a relay hash. The packet viewer gets it, so
                that the route graph of a relayed packet marks a repeater with ``▲`` (and
                the other types with their glyphs) instead of a dot.
            key_of: Gives the key of the node for the display name of a sender (refer to
                :func:`~meshterm.services.trace_runner.make_name_key_resolver`). Thus a
                channel sender that the contacts or the recorder know gets the hue of its
                key. A name that does not resolve stays muted.
            scope_of: Reads the scope of a flood from its raw packet, against the regions
                that have known names (``ctx.region_store.scope_of``). The scope lane uses
                it, and each viewer that opens gets it for its ``scope`` row. With
                ``None``, the screen still tells a scoped flood from a plain flood: it
                shows the code of a scoped flood in the place of the name
                (:func:`~meshterm.ui.packet_viewer.unnamed_scope`).
            message_scope: Reads the scope of a decoded channel message from its copies in
                the packet log (:func:`message_scope_reader`). Each viewer that opens gets
                it, so the card of a message has a ``scope`` row too. With ``None``, a
                message has no ``scope`` row.
        """
        super().__init__()
        self.title = "Live feed"
        self._session = session
        self._resolve = resolve
        self._prefix_bytes = prefix_bytes
        self._self_name = self_name
        self._channels = channels
        self._channel_names: Mapping[int, str] = channel_names or {}
        self._type_of = type_of
        self._key_of: NameKeyResolver = key_of or (lambda name: None)
        self._scope_of: ScopeOf = scope_of or unnamed_scope
        self._message_scope = message_scope
        #: The lanes of the last paint (refer to :func:`_lanes_for`). The subject builders
        #: read them, because their fallbacks for a full lane measure against the lane
        #: that was drawn.
        self._lanes = _lanes_for(0)
        #: The feed: the latest events of each class as data, with the newest first. The
        #: screen renders it again at each paint (the rows change with the width), and
        #: gives all of it to the viewer.
        self._feed: deque[PacketEntry] = deque(maxlen=_FEED_CAP)
        for obs in list(seed)[-_FEED_CAP:][::-1]:
            self._feed.append(PacketEntry.from_observation(obs))
        #: The highlighted feed row, or ``None`` when there is no row to highlight: an
        #: empty feed has no cursor stop. It starts on the newest packet.
        self._selected: int | None = 0 if self._feed else None
        #: Whether the cursor is on the *pin*: the stop above row 0, which holds the
        #: topmost position instead of a packet (refer to the module docstring). If it is
        #: true, ``_selected == 0`` is also true. The pin is a mode of the newest row, so
        #: all the code that reads the highlight (the list window fit, the viewer, the
        #: ``←→`` scroll) has no special case. Only the mark that the pointer lane draws is
        #: different. An empty feed has no row to pin to, and no cursor.
        self._pinned: bool = bool(self._feed)
        #: The list window of the feed in the fixed screen (its rows scroll under the
        #: heading).
        self._feed_window = ListWindow()
        #: The horizontal scroll of the highlighted row (the cells that ``←/→`` moved it),
        #: and how far it can move. The limit is measured against the width of the last
        #: paint, so a resize can only clamp a scroll that is in progress back into the
        #: range.
        self._hshift = 0
        self._hmax = 0

    # --- live feed -----------------------------------------------------------------

    def on_event(self, event: MeshEvent) -> None:
        """Add one hub event to the feed, and paint again.

        When a packet arrives, the two top stops of the screen behave differently. A
        selection is attached to a packet, so it moves down with that packet when the
        newer row comes in above it. The pin is attached to the position, so it stays at
        row 0 and the packet under it changes. That is the purpose of the pin. It is also
        the reason why this method resets the horizontal scroll of the highlighted row.
        The cursor is now on a different packet, the same as if the user pressed ``↓``,
        and each other move to a new row starts that row at its own beginning (refer to
        :data:`_HSHIFT_RESET`).
        """
        entry: PacketEntry | None = None
        obs = event.observation
        if obs is not None:
            entry = PacketEntry.from_observation(obs)
        elif event.kind == EventKind.MESSAGE and event.message is not None:
            msg = event.message
            entry = PacketEntry(
                when=utcnow(),
                kind="message",
                node=msg.sender,
                snr=msg.snr,
                where=f"ch {msg.channel}" if msg.is_channel else "direct",
                channel=msg.channel if msg.is_channel else None,
                text=msg.text,
                raw=msg.raw,
            )
        elif event.kind == EventKind.ACK and event.ack is not None:
            entry = PacketEntry(
                when=utcnow(),
                kind="ack",
                where=event.ack.code or "delivery confirmed",
            )
        if entry is None:
            return  # nothing new to show, so do not do a full paint for it
        self._feed.appendleft(entry)
        if self._pinned:
            self._selected = 0  # the pin holds the top, so this row is now the newest packet
            self._hshift = 0
        elif self._selected is not None:
            # Keep the highlight on the same packet when new rows push it down.
            self._selected = min(self._selected + 1, len(self._feed) - 1)
        self._session.invalidate()

    # --- input -----------------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The key hint. It gets ``←→ scroll line`` only while those keys have an effect.

        A row that fits already cannot scroll, so the hint does not show the atom for it.
        This is the same rule ("advertise a key only where it acts") that the ``hscroll``
        hint of the select list follows.
        """
        base = "↑↓ PgUp/PgDn Home/End move · Enter open · Esc back"
        if self._selected is not None and self._hmax > 0:
            return "↑↓ PgUp/PgDn Home/End move · ←→ scroll line · Enter open · Esc back"
        return base

    def handle(self, action: str, data: str = "") -> None:
        """Move the cursor, commit the row that it is on, scroll a row, or leave."""
        if action in _HSHIFT_RESET:
            self._hshift = 0  # a move from a row cancels its scroll
        if action == "up":
            self._move_selection(-1)
        elif action == "down":
            self._move_selection(1)
        elif action == "enter":
            self._commit()
        elif action == "pageup":
            # The page keys move the cursor by one list window at a time. Thus the
            # highlight moves with the list window, and the list window does not leave it
            # behind.
            self._select_stop(self._cursor - self._feed_window.page)
        elif action in ("pagedown", "space"):
            self._select_stop(self._cursor + self._feed_window.page)
        elif action in ("home", "ctrl_home"):
            # Home goes to the pin. The top of a live feed is the state that follows the
            # stream. Thus "take me back to the top" follows the stream again, and does not
            # stop on the packet that is the newest at this instant. End goes to the oldest
            # packet.
            self._select_stop(0)
        elif action in ("end", "ctrl_end"):
            self._select_stop(len(self._feed))
        elif action == "left":
            self._scroll_line(-_HSCROLL_STEP)
        elif action == "right":
            self._scroll_line(_HSCROLL_STEP)
        elif action == "escape":
            self.resolve(None)

    def _scroll_line(self, delta: int) -> None:
        """Move the highlighted row sideways (clamped to its own end), and paint again."""
        shift = max(0, min(self._hshift + delta, self._hmax))
        if shift != self._hshift:
            self._hshift = shift
            self._session.invalidate()

    @property
    def _cursor(self) -> int:
        """The cursor as one index over the stops of the screen: the pin, then the feed rows.

        The stops are the pin (0), then the feed rows (``1 .. len(feed)``). Thus each move
        is a clamp on this one number, the pin included. The pin and row 0 draw on the same
        line, but that is only a detail of the rendering. As cursor positions, they are
        two. That is why ``↓`` from the pin goes to the newest packet, and not to the
        second packet.

        An empty feed has no row to pin to, and thus no stops.
        """
        if self._pinned or self._selected is None:
            return 0
        return self._selected + 1

    def _move_selection(self, delta: int) -> None:
        """Move the cursor by ``delta`` stops (the two ends clamp on a packet)."""
        self._select_stop(self._cursor + delta)

    def _select_stop(self, index: int) -> None:
        """Move the cursor to one stop (the pin or a feed row), and paint again.

        Args:
            index: The stop to go to, clamped into the range (refer to :attr:`_cursor`).
                The two ends clamp and do not wrap, so a keyboard key that is held down
                stops at an end.
        """
        if not self._feed:  # nothing to pin or to select: the screen has no cursor
            self._pinned = False
            self._selected = None
            self._session.invalidate()
            return
        index = max(0, min(index, len(self._feed)))
        self._pinned = index == 0
        self._selected = max(0, index - 1)
        self._session.invalidate()

    def _select_row(self, row: int) -> None:
        """Put the cursor on feed row ``row``, selected in the usual way, and never on the pin.

        Other code uses this method to move the highlight (the packet viewer, when it pages
        through the feed under it). A move to a specific packet is always a selection and
        not a pin, also when that packet is the newest one. The user follows that packet,
        so the next packet that arrives must push it down, and must not take the cursor.
        """
        self._select_stop(row + 1)

    def _commit(self) -> None:
        """Enter: open the highlighted packet (an empty feed has none to open)."""
        self._open_packet()

    def _open_packet(self) -> None:
        """Open the packet viewer as a dialog over the highlighted feed row.

        When the user pages in the viewer, the feed highlight moves with it (through
        ``on_navigate``). Thus, when the viewer closes, the feed is on the packet that the
        user saw last. For the same reason, this method removes the pin when it opens the
        viewer. Enter names this packet. If the pin stays, the packets that arrive during a
        long read move the cursor off this packet. Then, when the viewer closes, the feed
        is on a row that the user did not select.
        """
        if self._selected is None or not self._feed:
            return
        self._select_row(self._selected)

        def follow(entry: PacketEntry) -> None:
            # The feed may have more rows after the snapshot. Thus find the entry itself.
            for i, candidate in enumerate(self._feed):
                if candidate is entry:
                    self._select_row(i)
                    return

        viewer = PacketViewer(
            list(self._feed),
            self._selected,
            resolve=self._resolve,
            prefix_bytes=self._prefix_bytes,
            self_name=self._self_name,
            on_navigate=follow,
            # The viewer has the second stop of this screen too. Thus, when the user goes
            # up to the stream in the dialog, the feed under it goes back to the pin, and
            # the feed still follows the stream when the dialog closes.
            on_pin=lambda: self._select_stop(0),
            channels=self._channels,
            type_of=self._type_of,
            key_of=self._key_of,
            scope_of=self._scope_of,
            message_scope=self._message_scope,
            # The live feed itself (the newest first). Thus the viewer shows the packets
            # that arrive while it is open, and does not stay at this snapshot.
            source=lambda: list(self._feed),
        )
        self._session.run_detached(self._session.run_screen(viewer))

    # --- rendering ---------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """The column header, then the list window of the feed.

        The header stays at the top because of the structure of this method, not because
        of the sticky-header mechanism of the base screen. The method first removes the
        header line from the viewport, and the list window of the feed is in the lines that
        remain (refer to :meth:`_feed_lines`). Thus the body never scrolls: the header
        cannot move off its top, and the oldest packet cannot go below its bottom.
        """
        if self._selected is not None:
            self._selected = min(self._selected, len(self._feed) - 1) if self._feed else None
        if self._selected is None:
            self._hmax = 0  # no highlighted row scrolls, so the hint does not show ←→
            self._pinned = False  # a feed with no rows has nothing to pin to
        lanes = self._lanes = _lanes_for(width)
        lines: list[str] = []
        if self._feed:  # a header over no rows is noise, and the empty note is sufficient
            lines.append(render_to_ansi(self._column_header(lanes), width, no_wrap=True))
        win = max(1, self._scroll_viewport - len(lines))
        lines.extend(self._feed_lines(width, win, lanes))
        self._scroll_total = max(1, len(lines))
        return lines

    @staticmethod
    def _column_header(lanes: _Lanes) -> Text:
        """The lane names, in the uppercase muted style of the column headers of the app.

        The header has the same lanes as :meth:`_feed_row`, the pointer column included,
        so each label is over the values that it names. Nothing here sorts. The feed is a
        stream, and its one order (the newest first) is the order of the ``TIME`` lane
        from top to bottom. Thus no column has the sort triangle of the contact list, or
        the white style of its active column. These labels are signs, not controls. The
        class lane never collapses (refer to :func:`_lanes_for`), so the header always
        shows its ``CLASS`` label.

        The label of the subject lane names the purpose of the lane, not the answer of one
        class: a packet is about a node for an advert, a channel for a channel text, and a
        pair of endpoints for a direct message. ``SUBJECT`` is the word that is correct for
        all of the column.
        """
        header = Text("  ", style="muted")  # the pointer lane
        header.append(fit_cells("TIME", _TIME_LANE))
        header.append(fit_cells("CLASS", _ICON_LANE + _FEED_CLASS_WIDTH + _LANE_GAP))
        header.append(fit_cells("SUBJECT", lanes.subject + _LANE_GAP))
        header.append(fit_cells("SCOPE", _SCOPE_LANE))
        # The label of each reading starts where its lane starts, as each other label on
        # the row does. When the label was right-aligned over the number, it ended in the
        # middle of the value, with the unit after it. It then seemed to float between two
        # columns, and did not name one of them.
        header.append(fit_cells("SNR", _SNR_LANE + _LANE_GAP))
        header.append(fit_cells("RSSI", _RSSI_LANE - _LANE_GAP))
        return header

    def _feed_lines(self, width: int, win: int, lanes: _Lanes) -> list[str]:
        """The visible feed rows: the newest first, with ``↑/↓ n more`` at the edges."""
        if not self._feed:
            return [render_to_ansi(Text("nothing heard yet", style="muted"), width)]
        entries = list(self._feed)
        top, count = self._feed_window.fit(len(entries), win, self._selected)
        out: list[str] = []
        if top > 0:
            out.append(render_to_ansi(ListWindow.marker(top, "above"), width))
        for i in range(top, top + count):
            row = self._feed_row(entries[i], i == self._selected, lanes, width)
            out.append(render_to_ansi(row, width, no_wrap=True))
        below = len(entries) - top - count
        if below > 0:
            out.append(render_to_ansi(ListWindow.marker(below, "below"), width))
        return out

    def _feed_row(self, entry: PacketEntry, selected: bool, lanes: _Lanes, width: int) -> Text:
        """Lay out one feed row in fixed lanes: time, class, subject, scope, reception, detail.

        The class lane tells what the packet is, directly from
        :func:`~meshterm.ui.packet_viewer.class_marks`. Thus a raw packet shows as
        ``📻 channel text`` (the class in the headline of the card in the viewer), not as
        the ``📦 packet`` event family in which it arrived. The name always shows. The
        icon next to it shows only where the platform draws icons
        (:func:`_bind_class_icon`). The subject lane next to the class lane then tells
        what the packet is about, in the terms of its class (:meth:`_feed_subject`). The
        row has no relay path. The lanes already use more than a 72-cell screen without
        one, and the packet viewer draws the route correctly, one key press away.

        The highlight is the usual highlight of the app: the ``❯`` pointer, and a
        ``cursor`` base style under all of the row, as each select list draws its
        highlight. The lanes keep their own colours over it (the SNR its quality colour,
        a name its palette hue), because those colours are the content. The pointer and
        the base tint are what show "this row". Only the newest row can have a different
        pointer: it gets ``^`` while the cursor is pinned to the top of the stream, and
        not to the packet that is there now (refer to :data:`_PIN_CURSOR` and to the
        module docstring). The lane, the cursor tint, and the one cell are the same. Only
        the mark is different, because only the meaning is different.

        If a row goes past the right edge (on a terminal that is narrower than the lanes),
        the user scrolls the row to read it, and the row does not get taller. The
        highlighted row moves sideways with ``←→`` (:attr:`_hshift`), as in the h-scroll
        convention of the app. The ``❯`` pointer lane does not move, so the pointer never
        scrolls away from the row that it marks. This method measures the limit of the
        shift against the current width, so a resize can only clamp the shift back into
        the range.
        """
        row = Text(no_wrap=True, overflow="ellipsis")
        cursor = _PIN_CURSOR if self._pinned else _CURSOR
        row.append(cursor if selected else "  ", style="cursor" if selected else "")
        avail = max(1, width - 2)

        body = Text(no_wrap=True, overflow="ellipsis")
        body.append(
            fit_cells(entry.when.astimezone().strftime("%H:%M:%S"), _TIME_LANE),
            style="muted",
        )
        icon, class_label = class_marks(entry)
        if _ICON_LANE:
            body.append(fit_cells(icon, _ICON_LANE))
        body.append(
            fit_cells(class_label, _FEED_CLASS_WIDTH),
            style=KIND_STYLES.get(entry.kind, "brand"),
        )
        body.append(" " * _LANE_GAP)  # the gutter of the lane, which the longest class fills
        subject = self._feed_subject(entry)
        # This lane fits as each other lane does: in cells, not characters, with an
        # ellipsis on overflow. But it is a Text, so a subject made of several styled
        # pieces keeps them.
        subject.truncate(lanes.subject, overflow="ellipsis", pad=True)
        body.append_text(subject)
        body.append(" " * _LANE_GAP)
        # Next to the subject, before the readings. The area into which a flood could go is
        # a fact about the packet, the same as its subject. The readings are about how the
        # packet got to us. A plain flood (most rows) is a muted dash instead of the word,
        # so the lane stays quiet until a row has a scope.
        scope = _feed_scope(self._entry_scope(entry))
        scope.truncate(_FEED_SCOPE_WIDTH, overflow="ellipsis", pad=True)
        body.append_text(scope)
        body.append(" " * _LANE_GAP)
        body.append(
            f"{entry.snr:+{_SNR_W}.1f} dB" if entry.snr is not None else " " * _SNR_LANE,
            style=snr_style(entry.snr) if entry.snr is not None else "muted",
        )
        body.append(
            f"{'':{_LANE_GAP}}{entry.rssi:{_RSSI_W}.0f} dBm"
            if entry.rssi is not None
            else " " * _RSSI_LANE,
            style="muted",
        )
        note = self._feed_note(entry)
        if note is not None:
            body.append(" " * _LANE_GAP)
            body.append_text(note)

        if selected:
            self._hmax = max(0, body.cell_len - avail)
            self._hshift = min(self._hshift, self._hmax)
            if self._hshift:
                body = crop_cells(body, self._hshift, avail)
        row.append_text(body)
        if selected:
            # The cursor tint is under the spans of the row (the convention of the select
            # list), so the lanes keep their colours and only the gaps get the tint.
            row.style = "cursor"
        return row

    def _feed_subject(self, entry: PacketEntry) -> Text:
        """What this packet is about, in the terms of its own class.

        The lane was a node lane before. That suited the two classes that name a node, but
        each other class showed ``—``. On a real mesh, that is half of the traffic: the
        direct messages, requests, responses, path returns, acks, and traces that are
        relayed past us and name no origin. Each of those packets is about something, but
        not about a node. Thus the lane asks each class its own question:

        * **advert, telemetry, a direct message that we received**: about the node that
          sent it, so the lane names the node
          (:func:`~meshterm.ui.packet_viewer.node_label`). The name has its palette hue,
          our node is white, and a bare hash is muted.
        * **channel text and channel data**: about the channel. The name comes from the
          key whose MAC confirms the packet
          (:func:`~meshterm.core.channels.identify_channel`). A channel for which we have
          no key shows the fingerprint that it advertises.
        * **direct message, request, response, returned path, anonymous request**: about
          the two nodes between which it travels, drawn as ``sender → recipient``
          (:meth:`_endpoints`).
        * **ack**: about the message that it acknowledges, named by the checksum of that
          message.
        * **trace**: about the walk that it collects, named by its tag.
        * **a channel message**: about the sender who signed it, or else the channel on
          which it arrived.

        The lane intentionally does not show the class of the packet. The class lane, two
        columns to the left, already shows it, directly from
        :func:`~meshterm.ui.packet_viewer.class_marks`. When a row showed the class two
        times, it read ``packet`` next to ``channel text``, as if they were two facts. A
        class that says nothing about itself still gets the dash, which is the true answer.

        Args:
            entry: The packet that the row describes.

        Returns:
            The content of the lane, not sized. The caller fits it to the lane.
        """
        label, style = node_label(entry, self._resolve, self._self_name)
        if label != "?":  # the packet named a node: that is what it is about
            return Text(label, style=style)
        if entry.kind == "message":
            return self._message_subject(entry)

        raw = entry.raw if isinstance(entry.raw, dict) else {}
        if raw.get("payload_typename") in CHANNEL_CLASSES:
            return self._channel_subject(raw)
        if raw.get("dest_hash"):
            return self._endpoints(raw)
        if raw.get("trace_tag"):
            return self._token("tag", raw["trace_tag"])
        if raw.get("ack_crc"):
            # An ack names no node. It identifies the message that it answers.
            return self._token("for", raw["ack_crc"])
        if entry.where:
            return Text(entry.where, style="muted")  # the code of a delivery ack
        return Text("—", style="muted")

    def _message_subject(self, entry: PacketEntry) -> Text:
        """A chat message's subject: its signer, or else the channel on which it arrived."""
        sender = _channel_sender(entry.text)
        if sender:
            ours = self._self_name and sender == self._self_name
            # A sender that resolves gets the hue of its key. An unknown sender stays
            # muted, because colour is only for identities that have a key.
            style = "you" if ours else name_style(sender, self._key_of(sender))
            return Text(sender, style=style)
        channel = self._channel_names.get(entry.channel) if entry.channel is not None else None
        if channel:  # an unsigned message: it is about the channel on which it arrived
            return Text(channel, style="brand")  # named the same as for an overheard packet
        if entry.where:
            return Text(entry.where, style="muted")  # …or the slot, if we cannot name it
        return Text("—", style="muted")

    def _channel_subject(self, raw: dict) -> Text:
        """A channel packet's subject: its channel, named by the key that its MAC confirms.

        The packet names its channel only by a one-byte fingerprint, and more than one
        channel can have the same fingerprint. Thus the lane shows a name only after a key
        that we have gives the same MAC as the packet
        (:func:`~meshterm.core.channels.identify_channel`), and never when only the
        fingerprint matches. A channel for which we have no key shows the fingerprint that
        the packet advertised, after the word of the app for a short derived id:
        ``hash a3``. This text is intentionally not ``ch a3``. The ``ch 3`` of a message
        row is a slot, and two different numbers with one prefix in the same column are
        worse than no text.
        """
        named = identify_channel(
            raw.get("chan_hash") or "",
            raw.get("cipher_mac") or "",
            raw.get("crypted") or "",
            self._channels,
        )
        if named is not None:
            return Text(named[0], style="brand")
        if raw.get("chan_hash"):
            return self._token("hash", raw["chan_hash"])
        return Text("—", style="muted")

    def _endpoints(self, raw: dict) -> Text:
        """An addressed packet's subject: the two nodes between which it travels.

        The subject is drawn as ``sender → recipient`` with the only hop-sequence widget
        (:mod:`~meshterm.ui.pathline`). Thus the endpoints of a delivery show the same as
        the hops of a route: resolved to contact names, each in the hue of its key, and
        our node in white when a packet is addressed to us (or from us). The widget uses
        plain arrows, not chips. This is a lane in a table row, and the padding of a chip
        uses the cells that are necessary for the names.

        The packet names each endpoint by one byte of its key. This is the same one-byte
        identity that a relay hop has, with the same probability of a collision. Thus a
        name here is a good guess, not a proof, and the hashes of the packet itself are
        always one key press away in the viewer. An anonymous request is the exception
        that helps us: it has the full key of its sender, which resolves exactly. If that
        key does not resolve, the lane shows it cut to the same one byte as all other
        text in the lane, instead of 64 hex digits that nobody can read quickly.

        If the two names do not fit in the lane, the recipient keeps its name and the
        sender changes to its hash. The packet is addressed, so the recipient is the half
        that gets the cells.
        """
        dest = raw.get("dest_hash") or ""
        src = raw.get("src_key") or raw.get("src_hash") or ""
        if not src:
            return self._token("to", dest)
        named = PathLine([self._hop(src), self._hop(dest)], mode="plain").text()
        if named.cell_len <= self._lanes.subject:
            return named
        # Too wide: the sender changes to its hash, so the recipient keeps all of its name
        # (or as much of it as the lane holds), and is not the half that gets cut.
        return PathLine([self._hop(src, named=False), self._hop(dest)], mode="plain").text()

    def _hop(self, value: str, *, named: bool = True) -> PathHop:
        """One endpoint as a path hop: its contact name, or its one-byte hash in its hue.

        Our node resolves to :data:`~meshterm.ui.pathline.SELF_GLYPH`, which all of the
        app uses, instead of to its name. It is the one endpoint that the user always
        knows. In an 18-cell lane, the cells that it does not use let the name at the
        other end show fully, and not cut.

        Args:
            value: The key hash of the endpoint. For the sender of an anonymous request,
                it is the full key, which is cut to the one-byte width of the lane when it
                has no name.
            named: Whether to resolve it. ``False`` forces the hash, for the fallback of a
                full lane, which gives the cells to the other end.
        """
        short = value[: 2 * ENDPOINT_HASH_BYTES]
        name = self._resolve(value) if named else None
        if name and name == self._self_name:
            return PathHop(SELF_GLYPH, you=True)
        if name and name != value:
            return PathHop(name, key=value)
        return PathHop(short, key=value, lit_bytes=ENDPOINT_HASH_BYTES)

    @staticmethod
    def _token(word: str, value: str) -> Text:
        """A token of the packet, after the one word that says what it is.

        A trace's tag, an ack's checksum, a channel's fingerprint: this hex identifies
        something, but it is not the identity of a node. Thus it gets no palette hue. The
        qualifier word is ``faint``, so it is less visible, and the value itself is muted
        but easy to read.
        """
        text = Text(f"{word} ", style="faint")
        text.append(value, style="muted")
        return text

    def _entry_scope(self, entry: PacketEntry) -> Scope | None:
        """The scope of the packet of a row, or ``None`` where it has no scope to show.

        Only a raw ``packet`` row has the route type and the transport codes from which a
        scope is read, and only a flood has a scope. For all other rows, the lane is
        blank. A direct packet is never filtered by region, so a word there gives it a
        meaning that it does not have. The lane is drawn ``bare``: its heading already
        says ``SCOPE``, so a known region shows as its name alone, and an unknown scope
        shows as ``? 3fa1`` (:func:`~meshterm.ui.widgets.scope_text`). A plain flood
        shows as a muted dash instead of ``unscoped`` (:func:`_feed_scope`).
        """
        if entry.kind != "packet" or not isinstance(entry.raw, dict):
            return None
        return self._scope_of(entry.raw)

    def _feed_note(self, entry: PacketEntry) -> Text | None:
        """The detail at the end of the row: the conversation of a message, and nothing else.

        Before, the route of a relayed packet was here, and it never fitted. The fixed
        lanes left only a small number of cells for it, so the chain was cut to a stub
        that named one hop and only suggested the others. A route is a shape, not a lane,
        and it is one key press away: the packet viewer draws it as a wrapped path line
        over the route graph, with the space to show all of it. Thus the feed tells what
        arrived, and Enter tells how it arrived.

        If the subject lane already shows the conversation, this detail does not show it
        again. An unsigned channel message shows under its channel, and a row that named
        the same conversation two times looked like two facts.

        Args:
            entry: The packet that the row describes.

        Returns:
            The detail to append, or ``None`` when the class has no detail.
        """
        if entry.kind != "message" or not entry.where:
            return None
        if entry.channel is not None and not _channel_sender(entry.text):
            return None  # the subject lane already shows the conversation
        return Text(entry.where, style="muted")


async def open_livefeed(ctx: AppContext) -> None:
    """Open the live feed, and run it until the user leaves it.

    This function connects the screen to its data sources. The recent stored observations
    are its first rows, a hub subscription sends each new packet to it, and a ticker
    paints it one time each second, so that the rows that show the time stay correct.
    When the screen closes, the function removes all of these.

    Args:
        ctx: The shared application context. The interactive TUI surface must be in use.

    Raises:
        RuntimeError: If the call is outside the interactive menu (there is no
            full-screen session).
    """
    from ..services import trace_runner
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the menu-only caller guards this
        raise RuntimeError("the live feed is only available in the menu")
    session = ctx.ui.session

    contacts = []
    self_name: str | None = None
    channels: list[tuple[str, bytes]] = []
    channel_names: dict[int, str] = {}
    slots_by_idx: dict[int, tuple[str, bytes]] = {}
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            # Through the session cache. The contacts and the channel-slot probe are the
            # two slowest reads when a screen opens. Thus MeshTerm reads them one time (not
            # each time that the feed opens), and navigation stays fast over Bluetooth.
            contacts = await ctx.devstate.contacts()
            self_name = (await ctx.devstate.self_info()).get("name") or None
            slots = await ctx.devstate.channel_slots()
            channels = [(s.name, s.secret) for s in slots]
            channel_names = {s.idx: s.name for s in slots}
            slots_by_idx = {s.idx: (s.name, s.secret) for s in slots}
    except Exception:  # noqa: BLE001 - the feed renders correctly without contact names
        contacts = []
    # The contacts first, then each name that the recorder overheard, as the fallback.
    # This is the rule of all of the app: a node that we can name never renders as a bare
    # hash.
    stored_names = ctx.repo.node_names()
    resolve = trace_runner.make_node_resolver(contacts, stored_names)
    type_of = trace_runner.make_node_type_resolver(contacts)
    key_of = trace_runner.make_name_key_resolver(contacts, stored_names)
    prefix_bytes = await ctx.devstate.routing_prefix_bytes()

    # The cap is the only limit: the newest _FEED_CAP packets that were ever stored, also
    # when the quiet period before them started a long time ago.
    seed = ctx.repo.recent_observations(since=_FEED_SEED_FLOOR, limit=_FEED_CAP)
    screen = LiveFeedScreen(
        session=session,
        resolve=resolve,
        seed=seed,
        prefix_bytes=prefix_bytes,
        self_name=self_name,
        channels=channels,
        channel_names=channel_names,
        type_of=type_of,
        key_of=key_of,
        scope_of=ctx.region_store.scope_of,
        message_scope=message_scope_reader(ctx, slots_by_idx),
    )

    unsubscribe = ctx.events.subscribe(screen.on_event)

    async def tick() -> None:
        """Paint one time each second, so that the rows that show the time stay correct."""
        while True:
            await asyncio.sleep(_REFRESH_S)
            session.invalidate()

    ticker = asyncio.ensure_future(tick())
    try:
        await session.run_screen(screen)
    finally:
        unsubscribe()
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - the teardown must never show an error of a tick
            pass


def message_scope_reader(
    ctx: AppContext, slots_by_idx: Mapping[int, tuple[str, bytes]]
) -> MessageScopeOf:
    """A reader for a decoded channel message's scope, from its copies in the packet log.

    The device gives a received message to MeshTerm as text, not as the packet in which
    it arrived. Thus a ``message`` row has no transport code. But its flooded copies are
    in the packet log, and the message paths dialog already finds them: it decrypts them
    and matches the text (:func:`~meshterm.services.message_paths.channel_arrivals`).
    Each copy has the one code of the sender, so the scope of the copies is the scope of
    the message (:func:`~meshterm.services.message_paths.message_scope`).

    The reader keeps the copies that it found for each entry, because a card paints again
    each second, and the search is a query over a time window plus one decrypt for each
    packet. But the reader keeps them only after it found some. The feed can get a message
    a short time before its packet is written to the log. If the reader cached a miss at
    that time, the miss stays a miss permanently. The reader resolves the region at each
    call, against the cache of the store. Thus a name that MeshTerm learns while the card
    is open still names the region.

    A direct message has no reader: its copies are encrypted end to end, and a routed
    direct message has no scope.

    Args:
        ctx: The application context (its repository and region store).
        slots_by_idx: The channels of the device by slot, as ``(name, secret)``.

    Returns:
        The reader. It returns ``None`` for each entry that it cannot place.
    """
    from ..core.models import ChatMessage
    from ..services.message_paths import channel_arrivals, message_scope

    found: dict[int, list] = {}

    def read(entry: PacketEntry) -> Scope | None:
        if entry.kind != "message" or entry.channel is None:
            return None
        channel = slots_by_idx.get(entry.channel)
        if channel is None or not entry.text:
            return None
        arrivals = found.get(id(entry))
        if arrivals is None:
            message = ChatMessage(
                text=entry.text, outbound=False, is_channel=True, created_at=entry.when
            )
            try:
                arrivals = channel_arrivals(
                    ctx.repo, message, channel_name=channel[0], secret=channel[1]
                )
            except Exception:  # noqa: BLE001 - a card with no scope row, but never a crash
                return None
            if arrivals:
                found[id(entry)] = arrivals
        return message_scope(arrivals, ctx.region_store.scope_of)

    return read
