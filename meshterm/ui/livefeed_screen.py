# SPDX-License-Identifier: Apache-2.0
"""The Live feed: every packet as it arrives, full-screen and newest first.

The interactive face of the ``livefeed`` tool — the dashboard's old feed panel,
promoted to a first-class screen. One always-repainting list streams the latest
packets, newest first: time, class (its name, beside an icon where the platform draws one),
subject, SNR/RSSI, and a message's conversation. What a row deliberately does *not* carry
is the relay path a frame rode in on: a route is a shape, not a lane, and squeezing one
into the cells left at the right edge only ever produced a stub — so the route belongs
to the packet viewer, which has the room to draw it as a wrapped path line over its
graph. The feed's job is to say what arrived and how well it was heard; Enter says how
it got here.

A **scope** lane sits between the subject and the readings, at every width: the region a
flood was sent into (``harbour``), ``? 3fa1`` for a region nobody here has named, a muted
``-`` for a plain flood, and nothing for a direct frame, which no repeater region-filters.
It never collapses; the class label beside the icon is what gives way on a narrow terminal
(see :func:`_lanes_for`).

The subject lane is contextual: it holds whatever the *class* of packet is about (see
:meth:`LiveFeedScreen._feed_subject`). An advert or a telemetry frame is about the node
that sent it, so the lane names the node — but a channel text is about its channel, a
direct message or a request about the two nodes it travels between, a trace about its
tag. Only a class that genuinely says nothing about itself leaves the lane empty.

Seeded from stored history so the screen opens full, then streamed
live off the event hub. ``↑``/``↓`` walk the rows, PgUp/PgDn/Home/End page and jump
them, and ``←``/``→`` scroll the highlighted row sideways on a terminal too narrow to
hold it whole.

The top of the list is **two stops, not one** — the difference between watching the feed
and reading it. The first is the *pin*: it holds the very topmost position, so every
packet that arrives takes the highlight with it, and the cursor draws as ``^`` (pointing
past the list's top, at the traffic still to come) rather than the app's ``❯``. One
``↓`` off it does not move down a row — it lands on that same newest packet, now
*normally* selected under a plain ``❯``, and from there the highlight belongs to that
packet and rides down as newer ones push it along. So the pin follows the stream and a
selection follows a packet, and the mark in the cursor lane says which you are doing.
The screen opens pinned (a live feed's resting state is *watching*), ``↑`` from the newest
packet re-pins, and Home jumps straight back to the pin from anywhere.

Enter opens the highlighted packet in the shared
:class:`~meshterm.ui.packet_viewer.PacketViewer` — which then pages through the feed
itself with the same ``↑``/``↓``, and, for an overheard channel-text packet naming a
channel we hold the key for, decrypts it. **Both stops go up with it**: the viewer holds
the same pin above the newest packet, so ``↑`` off the top follows the stream there too
(each arrival becoming the card being read), and it walks the feed's cursor in step, pin
included — closing the dialog lands on whatever the reader was doing inside it rather
than on a state they left behind at the door.

The screen holds no subscriptions of its own — the opener (:func:`open_livefeed`)
wires the hub subscription and the once-a-second repaint, and tears them down when
the screen resolves. The newest packet is highlighted from the moment the screen
opens; Esc backs out directly.
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

#: Seconds between full repaints while the feed is open (keeps age-sensitive chrome
#: honest even between hub events).
_REFRESH_S = 1.0

#: How many feed rows are kept (the feed windows within the screen, so this is
#: history depth, not layout) — and, being the feed's *only* bound, the one number that
#: decides how far back the screen remembers. A busy hour on a real mesh runs to some
#: hundreds of packets, so a hundred rows forgot the last ten minutes exactly when the
#: feed was worth watching. Depth is nearly free here: the paint is windowed
#: (:meth:`LiveFeedScreen._feed_lines` formats only the rows on screen), so the cap costs
#: a pointer per entry and a few milliseconds of hydration at open, never anything per
#: frame.
_FEED_CAP = 500

#: How far back the opening seed may reach: **no limit** — the cap alone says how much
#: history the feed holds, and a clock has no business overruling it. A quiet mesh would
#: otherwise open a half-empty screen not because nothing was ever heard but because
#: nothing was heard *lately*, which is the one thing the reader can already see.
#:
#: The mechanics are unchanged — :meth:`~meshterm.persistence.repository.Repository
#: .recent_observations` still takes a ``since``, and this is simply a floor no stored
#: row predates. It stays cheap because ``observed_at`` is indexed: the query walks the
#: index backwards and stops after :data:`_FEED_CAP` rows, so widening the window reads
#: no more rows than narrowing it did. Deliberately *not*
#: :data:`~meshterm.persistence.repository.OBSERVATION_WINDOW`, which the feed used to
#: borrow: that one bounds the dashboard's rolling stats and is what its RF health card
#: means by "reception over 2 h".
_FEED_SEED_FLOOR = datetime.min.replace(tzinfo=timezone.utc)

#: The feed's fixed subject-lane width; anything longer ellipsizes so the columns hold.
#: Sized for a node name, which is what most classes put here — and wide enough that the
#: two endpoints of an addressed frame still read as ``a1 → Hilltop-Repe…`` rather than
#: collapsing to bare hashes. Widening it would push the row past the 72-column screen
#: the lanes are budgeted for, so the cells the lane can't hold are the viewer's to spend.
_FEED_SUBJECT_WIDTH = 18

#: The class lane's fixed width — wide enough for the longest class the app files a
#: packet under (``telemetry``, ``chan text``, ``multipart``), so every label reads whole.
#: The names are kept short on purpose (``dir messg``, ``anon req`` — JP, 2026-09-25): the
#: lane never collapses, so every cell it spends is one the row's readings scroll off for.
_FEED_CLASS_WIDTH = 9

# The remaining lanes, as the row and its column header both measure them — one set of
# numbers so a header label can never drift off the values it names, and so a lane a
# packet left empty pads to exactly the width it would have filled.
#: Cells between adjacent lanes.
_LANE_GAP = 2
#: ``HH:MM:SS`` plus the gap that follows it.
_TIME_LANE = 8 + _LANE_GAP
#: The class icon and its trailing space — or nothing, where the platform draws no icons
#: (bound by :func:`_bind_class_icon`).
_ICON_LANE = 3


@on_platform
def _bind_class_icon(platform: Platform) -> None:
    """Bind the class icon lane to the platform (runs now and on every switch).

    The class *name* is always drawn (it never collapses — JP, 2026-09-25), so the icon
    beside it is a family cue rather than the information, which is exactly what
    :attr:`~meshterm.platforms.Platform.menu_icons` governs: on the PicoCalc, where a class
    icon can only be a one-glyph stand-in, the lane is dropped and its three cells go to
    the row.
    """
    global _ICON_LANE
    _ICON_LANE = 3 if platform.menu_icons else 0


#: Cells a reception reading right-aligns its number in — the ``%+5.1f``/``%5.0f`` field
#: both lanes are built on, and the slot their column headers sit over.
_READING_W = 5
#: ``+13.2 dB`` — the number field, then its unit.
_SNR_LANE = _READING_W + len(" dB")
#: ``    -61 dBm`` — the lane gap, the number field, then its unit.
_RSSI_LANE = _LANE_GAP + _READING_W + len(" dBm")

#: The scope lane's width. Room for an unnamed scope's ``? 3fa1`` and a short region name
#: whole; a longer name ellipsizes — its whole name is on the viewer's ``scope`` row, one
#: keypress away, like everything else a lane cuts.
_FEED_SCOPE_WIDTH = 10

#: The scope lane with the gap that sets it off from the readings after it.
_SCOPE_LANE = _FEED_SCOPE_WIDTH + _LANE_GAP


def _feed_scope(scope: Scope | None) -> Text:
    """One row's scope cell: the region, ``? 3fa1`` unnamed, a muted dash for unscoped.

    Blank for a direct frame (or a row that is no frame at all), which has no scope: a
    dash there would claim a plain flood it never was.
    """
    if scope is not None and not scope.scoped:
        return Text("-", style="muted")
    return scope_text(scope, bare=True)


class _Lanes(NamedTuple):
    """The lane widths one paint lays its rows out in.

    Attributes:
        subject: The subject lane's width (:data:`_FEED_SUBJECT_WIDTH`).
    """

    subject: int


def _lanes_for(width: int) -> _Lanes:
    """The lanes a row carries at ``width`` — all of them, at every width.

    No lane collapses (JP, 2026-09-25): the class name and the scope were each dropped on
    a narrow terminal in turn, and each time the lane that went was one a reader came to
    the feed for. A row wider than the screen is read the app-wide way instead — the
    highlighted row slides sideways under ``←→`` — which on a 72-column terminal reaches
    the readings at the row's end, and on the PicoCalc's 53 everything past the subject.
    """
    return _Lanes(_FEED_SUBJECT_WIDTH)


#: Cells one ←/→ press shifts the highlighted row by — the app-wide select list's own
#: step (:attr:`~meshterm.ui.tui.select.SelectScreen._HSCROLL_STEP`), so a row here
#: scrolls at the rate a row anywhere else does.
_HSCROLL_STEP = 8

#: Moves that abandon the highlighted row's horizontal scroll: each row scrolls on its
#: own, exactly as an ``hscroll`` select list's rows do — landing on a new packet always
#: starts it at its own beginning.
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

#: The cursor over a normally selected row — the app-wide select-list pointer, pointing
#: *into* the list at the one row it marks.
_CURSOR = "❯ "

#: The cursor over the pinned newest row (see the module docstring). The same lane, turned
#: to point past the top of the list — at the traffic still to arrive — because that is
#: what the pin is attached to: not this packet, but whichever one is newest. It is
#: deliberately not ``↑``, which this screen already spends on the window's ``↑ n more``
#: marker and would read here as "there is more above" — the one thing the top of the feed
#: can never mean; nor ``▲``, which is the repeater in the app's node-type set. A caret is
#: unclaimed, reads as "topmost" on both platforms, and is plain ASCII, so it needs no
#: entry in the console font's glyph map (see :mod:`meshterm.ui.fontset`).
_PIN_CURSOR = "^ "


def _channel_sender(text: str | None) -> str | None:
    """The sender named by a channel message's ``Name: `` prefix, or ``None`` if absent.

    The shared protocol-layer parse (see
    :func:`~meshterm.core.channels.split_channel_sender`), so the feed's node lane names
    exactly the sender the chat transcript would.
    """
    name, _body = split_channel_sender(text or "")
    return name


class LiveFeedScreen(Screen):
    """The full-screen packet stream. Renders state; the opener feeds it."""

    floating = False

    @property
    def fkey_lane(self):
        """The shared lane, gated on there being packets to walk.

        Every nav key here moves the *cursor* over the feed rather than a scroll offset,
        but the vocabulary is the same either way — the window follows the highlight — so
        the shared labels stand. ``Top`` and ``Bottom`` stay literal on purpose: the top of
        this list is the pin riding the newest packet and its bottom is the oldest one,
        which is where End actually lands. ``Top`` is how the pin is reached where there is
        no hint line to name it — a feed read down into history resumes following in one
        chip.

        A feed with nothing in it has no cursor and nowhere to move to, so all four chips
        go dim until the first packet arrives.
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
        """Create the feed over its data feeds.

        Args:
            session: The running TUI session (for repaints and the packet viewer).
            resolve: Maps a node hash to a friendly contact name when known.
            seed: Stored observations to open with (oldest first, as the repository
                returns them); the newest :data:`_FEED_CAP` become the opening feed.
            prefix_bytes: The hash width to light in the packet viewer's keys.
            self_name: Our own node's name, drawn white wherever it appears.
            channels: The device's configured channels, as ``(name, secret)`` pairs. The
                subject lane names an overheard channel frame's channel by whichever of
                these keys reproduces its MAC, and each opened
                :class:`~meshterm.ui.packet_viewer.PacketViewer` decrypts with them.
            channel_names: The device's channel names by slot index, so a channel message
                the radio decoded for us can be filed under the same channel name an
                overheard frame on it is — rather than under its slot number.
            type_of: Maps a relay hash to its node type, handed to the packet viewer so a
                relayed packet's route graph marks a repeater ``▲`` (etc.) over a dot.
            key_of: Maps a sender's display name back to its node's key (see
                :func:`~meshterm.services.trace_runner.make_name_key_resolver`), so a
                channel sender the contacts or the recorder know takes its key-derived
                hue; an unresolvable name stays muted.
            scope_of: Reads a flood's scope off its raw frame against the regions known by
                name (``ctx.region_store.scope_of``) — for the scope lane, and handed to
                each opened viewer for its ``scope`` row. ``None`` still tells a scoped
                flood from a plain one, showing a scoped one's code where a name would go
                (:func:`~meshterm.ui.packet_viewer.unnamed_scope`).
            message_scope: Reads a decoded channel message's scope off its copies in the
                packet log (:func:`message_scope_reader`), handed to each opened viewer so
                a message's card has a ``scope`` row too. ``None``: no row for messages.
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
        #: The lanes the last paint carried (see :func:`_lanes_for`) — read by the subject
        #: builders, whose crowded-lane fallbacks measure against the lane actually drawn.
        self._lanes = _lanes_for(0)
        #: The feed: latest events of every class as data, newest first — rendered
        #: fresh each paint (rows adapt to width) and handed whole to the viewer.
        self._feed: deque[PacketEntry] = deque(maxlen=_FEED_CAP)
        for obs in list(seed)[-_FEED_CAP:][::-1]:
            self._feed.append(PacketEntry.from_observation(obs))
        #: The highlighted feed row, or ``None`` when there is no row to highlight —
        #: an empty feed, which offers no cursor stop at all. Starts on the newest packet.
        self._selected: int | None = 0 if self._feed else None
        #: Whether the cursor is on the *pin* — the stop above row 0, which holds the
        #: topmost position rather than a packet (see the module docstring). Implies
        #: ``_selected == 0``: the pin is a mode on the newest row, so everything that
        #: reads the highlight (the window fit, the viewer, the ``←→`` scroll) needs no
        #: special case; only the cursor mark drawn in the pointer lane differs. A feed
        #: with nothing in it has no row to pin to, and no cursor at all.
        self._pinned: bool = bool(self._feed)
        #: The feed's window within the fixed screen (its rows scroll under the heading).
        self._feed_window = ListWindow()
        #: The highlighted row's horizontal scroll (cells shifted in, ``←/→``) and how
        #: far it can shift — measured against the width of the last paint, so a resize
        #: can only ever clamp an in-progress scroll back into range.
        self._hshift = 0
        self._hmax = 0

    # --- live feed -----------------------------------------------------------------

    def on_event(self, event: MeshEvent) -> None:
        """Fold one hub event into the feed and repaint.

        The arrival is where the screen's two top stops part company. A *selection* is
        attached to a packet, so it rides down with it as the newer row pushes in above.
        The *pin* is attached to the position, so it stays at row 0 and the packet under
        it changes — which is the whole point of it, and also why the highlighted row's
        horizontal scroll is dropped here: the cursor has landed on a different packet
        just as surely as if the reader had pressed ``↓``, and every other move onto a new
        row starts that row at its own beginning (see :data:`_HSHIFT_RESET`).
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
            return  # nothing new on screen — don't buy a full repaint for it
        self._feed.appendleft(entry)
        if self._pinned:
            self._selected = 0  # the pin holds the top: the newest packet is now this one
            self._hshift = 0
        elif self._selected is not None:
            # Keep the highlight on the same packet as new rows push it down.
            self._selected = min(self._selected + 1, len(self._feed) - 1)
        self._session.invalidate()

    # --- input -----------------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The key hint, gaining ``←→ scroll line`` exactly while it would do something.

        A row that already fits scrolls nowhere, so the atom stays hidden on it — the
        same "advertise a key only where it acts" rule the select list's own ``hscroll``
        hint follows.
        """
        base = "↑↓ PgUp/PgDn Home/End move · Enter open · Esc back"
        if self._selected is not None and self._hmax > 0:
            return "↑↓ PgUp/PgDn Home/End move · ←→ scroll line · Enter open · Esc back"
        return base

    def handle(self, action: str, data: str = "") -> None:
        """Walk the cursor, commit the row it rests on, scroll a row, or dismiss."""
        if action in _HSHIFT_RESET:
            self._hshift = 0  # moving off a row abandons its scroll
        if action == "up":
            self._move_selection(-1)
        elif action == "down":
            self._move_selection(1)
        elif action == "enter":
            self._commit()
        elif action == "pageup":
            # The page keys walk the cursor a windowful at a time, so the highlight
            # travels with the window rather than being left behind by it.
            self._select_stop(self._cursor - self._feed_window.page)
        elif action in ("pagedown", "space"):
            self._select_stop(self._cursor + self._feed_window.page)
        elif action in ("home", "ctrl_home"):
            # Home jumps to the pin — the top of a live feed is the *following* state, so
            # "take me back to the top" resumes the stream rather than parking on whichever
            # packet happens to be newest this instant. End goes to the oldest packet.
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
        """Shift the highlighted row sideways (clamped to its own tail) and repaint."""
        shift = max(0, min(self._hshift + delta, self._hmax))
        if shift != self._hshift:
            self._hshift = shift
            self._session.invalidate()

    @property
    def _cursor(self) -> int:
        """The cursor as one index over the screen's stops: the pin, then the feed's rows.

        The stops are the pin (0), then the feed's rows (``1 .. len(feed)``) — so every
        move stays a clamp on this one number, the pin included. That the pin and row 0
        draw on the *same line* is a rendering detail; as cursor positions they are two,
        which is exactly what makes ``↓`` off the pin land on the newest packet rather
        than the second one.

        An empty feed has no row to pin to, and so no stops at all.
        """
        if self._pinned or self._selected is None:
            return 0
        return self._selected + 1

    def _move_selection(self, delta: int) -> None:
        """Step the cursor by ``delta`` stops (both ends clamp on a packet)."""
        self._select_stop(self._cursor + delta)

    def _select_stop(self, index: int) -> None:
        """Move the cursor to one stop — the pin or a feed row — and repaint.

        Args:
            index: The stop to land on, clamped into range (see :attr:`_cursor`). Both ends
                clamp rather than wrap, so a key held down settles at an end.
        """
        if not self._feed:  # nothing to pin or select: the screen has no cursor
            self._pinned = False
            self._selected = None
            self._session.invalidate()
            return
        index = max(0, min(index, len(self._feed)))
        self._pinned = index == 0
        self._selected = max(0, index - 1)
        self._session.invalidate()

    def _select_row(self, row: int) -> None:
        """Put the cursor on feed row ``row``, normally selected — never on the pin.

        The way *something else* moves the highlight (the packet viewer paging through the
        feed under it). Landing on a specific packet is by definition a selection and not a
        pin, even when that packet happens to be the newest one: the reader is following
        that packet, so the next arrival must push it down rather than steal the cursor.
        """
        self._select_stop(row + 1)

    def _commit(self) -> None:
        """Enter: open the highlighted packet (an empty feed has none to open)."""
        self._open_packet()

    def _open_packet(self) -> None:
        """Float the packet viewer over the highlighted feed row.

        Paging inside the viewer walks the feed highlight in step (via ``on_navigate``), so
        closing it lands back on the packet last viewed — and opening one drops the pin for
        the same reason. Enter names *this* packet; if the pin outlived it, the arrivals
        during a long read would walk the cursor off it, and closing the viewer would land
        somewhere the reader never chose.
        """
        if self._selected is None or not self._feed:
            return
        self._select_row(self._selected)

        def follow(entry: PacketEntry) -> None:
            # The feed may have grown since the snapshot; find the entry itself.
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
            # The viewer carries this screen's own second stop, so a reader who walks up
            # to the stream inside the dialog re-pins the feed under it and is still
            # following when the dialog closes.
            on_pin=lambda: self._select_stop(0),
            channels=self._channels,
            type_of=self._type_of,
            key_of=self._key_of,
            scope_of=self._scope_of,
            message_scope=self._message_scope,
            # The live feed itself (newest first), so the viewer keeps up with packets
            # that arrive while it is open instead of freezing at this snapshot.
            source=lambda: list(self._feed),
        )
        self._session.run_detached(self._session.run_screen(viewer))

    # --- rendering ---------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """The column header, then the feed's window.

        The header is pinned by construction rather than by the base screen's sticky-header
        machinery: it is struck from the viewport first and the feed windows *inside* what
        is left (see :meth:`_feed_lines`), so the body never scrolls — the header can't
        travel off the top of it, and the oldest packet can't sink past the bottom.
        """
        if self._selected is not None:
            self._selected = min(self._selected, len(self._feed) - 1) if self._feed else None
        if self._selected is None:
            self._hmax = 0  # nothing highlighted scrolls, so nothing advertises ←→
            self._pinned = False  # a feed with no rows has nothing to pin to
        lanes = self._lanes = _lanes_for(width)
        lines: list[str] = []
        if self._feed:  # a header over nothing is noise; the empty note speaks for itself
            lines.append(render_to_ansi(self._column_header(lanes), width, no_wrap=True))
        win = max(1, self._scroll_viewport - len(lines))
        lines.extend(self._feed_lines(width, win, lanes))
        self._scroll_total = max(1, len(lines))
        return lines

    @staticmethod
    def _column_header(lanes: _Lanes) -> Text:
        """The lane names, in the app's uppercase muted column-header voice.

        Laid out lane for lane against :meth:`_feed_row`, the pointer column included, so
        every label sits over the values it names. Nothing here sorts — the feed is a
        stream, and its one order (newest first) is what the ``TIME`` lane reads down — so
        no column carries the contact list's sort triangle or its lit active-column style;
        these are signposts, not controls. On a terminal too narrow for the class label
        the header drops its ``CLASS`` too, leaving the icon lane unlabelled rather than
        clipping a word into three cells.

        The subject lane's label names the lane's *job*, not one class's answer to it:
        what a packet is about is a node for an advert, a channel for a channel text, a
        pair of endpoints for a direct message. ``SUBJECT`` is the word that stays true
        down the whole column.
        """
        header = Text("  ", style="muted")  # the pointer lane
        header.append(fit_cells("TIME", _TIME_LANE))
        header.append(fit_cells("CLASS", _ICON_LANE + _FEED_CLASS_WIDTH + _LANE_GAP))
        header.append(fit_cells("SUBJECT", lanes.subject + _LANE_GAP))
        header.append(fit_cells("SCOPE", _SCOPE_LANE))
        # The two readings right-align their number, so their labels do too — each sits
        # over the digits it names rather than over the sign column ahead of them.
        header.append(fit_cells("SNR", _READING_W, align="right"))
        header.append(" " * (_SNR_LANE - _READING_W))
        header.append(" " * _LANE_GAP)
        header.append(fit_cells("RSSI", _READING_W, align="right"))
        header.append(" " * (_RSSI_LANE - _LANE_GAP - _READING_W))
        return header

    def _feed_lines(self, width: int, win: int, lanes: _Lanes) -> list[str]:
        """The feed's windowed rows: newest first, ``↑/↓ n more`` at the edges."""
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
        """Lay one feed row out in fixed lanes: time, class, subject, reception, detail.

        The class lane says what the packet *is*, straight from
        :func:`~meshterm.ui.packet_viewer.class_marks` — so a raw frame reads
        ``📻 channel text``, the class the viewer's card headlines, rather than the
        ``📦 packet`` event family it merely arrived in. The name always shows; the icon
        beside it only where the platform draws icons (:func:`_bind_class_icon`). The
        subject lane beside it
        then answers what the packet is *about*, in whatever terms its class deals in
        (:meth:`_feed_subject`). No relay path rides here — the whole row fits a
        72-column screen precisely because it doesn't try to, and the route is drawn
        properly one keypress away, in the packet viewer.

        The highlight is the app's own: the ``❯`` pointer and a ``cursor`` base under the
        whole row, exactly as every select list draws its cursor. The lanes keep their own
        colours over it — the SNR its quality hue, a name its palette hue — because those
        colours are the content; the pointer and the base tint are what say "this row".
        The one row that can wear a *different* pointer is the newest, which takes ``^``
        while the cursor is pinned to the top of the stream rather than to the packet
        currently sitting there (:data:`_PIN_CURSOR`, and the module docstring). Same lane,
        same cursor tint, same one cell — only the mark differs, because only the meaning
        does.

        A row that runs past the right edge — a terminal narrower than the lanes need —
        is read by scrolling it, not by growing it: the *highlighted* row slides under
        ``←→`` (:attr:`_hshift`), the app-wide h-scroll convention, with the ``❯``
        pointer lane pinned so the cursor never scrolls away from the row it marks. The
        shift bound is measured here, against the current width, so a resize can only
        clamp it back into range.
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
        body.append(" " * _LANE_GAP)  # the lane's gutter — the longest class fills it
        subject = self._feed_subject(entry)
        # The lane fits like every other: cells, not characters, ellipsis on overflow —
        # but as a Text, so a subject built of several styled pieces keeps them.
        subject.truncate(lanes.subject, overflow="ellipsis", pad=True)
        body.append_text(subject)
        body.append(" " * _LANE_GAP)
        # Beside the subject, ahead of the readings: where a flood was allowed to go is a
        # fact about the packet, as its subject is, and the readings are about how it
        # reached us. A plain flood — most rows — is a muted dash rather than the word, so
        # the lane is quiet until a row is actually scoped.
        scope = _feed_scope(self._entry_scope(entry))
        scope.truncate(_FEED_SCOPE_WIDTH, overflow="ellipsis", pad=True)
        body.append_text(scope)
        body.append(" " * _LANE_GAP)
        body.append(
            f"{entry.snr:+{_READING_W}.1f} dB" if entry.snr is not None else " " * _SNR_LANE,
            style=snr_style(entry.snr) if entry.snr is not None else "muted",
        )
        body.append(
            f"{'':{_LANE_GAP}}{entry.rssi:{_READING_W}.0f} dBm"
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
            # The cursor tint rides *under* the row's spans (the select list's own
            # convention), so the lanes keep their colours and only the gaps take it.
            row.style = "cursor"
        return row

    def _feed_subject(self, entry: PacketEntry) -> Text:
        """What this packet is *about*, in the terms its own class deals in.

        The lane used to be a node lane, which suited the two classes that name a node and
        left every other one reading ``—``: on a real mesh that is half the traffic —
        the direct messages, requests, responses, path returns, acks and traces that
        relay past us naming no origin. Each of those is about *something*, just not
        about a node, so the lane asks each class its own question:

        * **advert, telemetry, a direct message we received** — about the node that sent
          it, so the lane names the node (:func:`~meshterm.ui.packet_viewer.node_label`):
          its palette hue, our own node white, a bare hash muted.
        * **channel text and channel data** — about the *channel*, named by the key whose
          MAC confirms the frame (:func:`~meshterm.core.channels.identify_channel`); a
          channel we hold no key for falls back to the fingerprint it advertises.
        * **direct message, request, response, returned path, anonymous request** —
          about the pair of nodes it travels between, drawn as ``sender → recipient``
          (:meth:`_endpoints`).
        * **ack** — about the message it acknowledges, named by that message's checksum.
        * **trace** — about the walk it is collecting, named by its tag.
        * **a channel message** — about whoever signed it, else the channel it arrived on.

        What the lane deliberately does *not* do is stand in the packet's class: the class
        lane two columns left already says that, straight from
        :func:`~meshterm.ui.packet_viewer.class_marks`, and a row that spelled it twice
        read ``packet`` beside ``channel text`` as though they were two facts. A class that
        genuinely says nothing about itself still gets the honest dash.

        Args:
            entry: The packet the row describes.

        Returns:
            The lane's content, unsized — the caller fits it to the lane.
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
            # An ack names no node at all — what it identifies is the message it answers.
            return self._token("for", raw["ack_crc"])
        if entry.where:
            return Text(entry.where, style="muted")  # a delivery ack's code
        return Text("—", style="muted")

    def _message_subject(self, entry: PacketEntry) -> Text:
        """A chat message's subject: whoever signed it, else the channel it arrived on."""
        sender = _channel_sender(entry.text)
        if sender:
            ours = self._self_name and sender == self._self_name
            # A resolvable sender takes its key-derived hue; a stranger stays muted —
            # colour is reserved for keyed identities.
            style = "you" if ours else name_style(sender, self._key_of(sender))
            return Text(sender, style=style)
        channel = self._channel_names.get(entry.channel) if entry.channel is not None else None
        if channel:  # an unsigned post: the channel it landed on is what it is about
            return Text(channel, style="brand")  # named exactly as an overheard frame's is
        if entry.where:
            return Text(entry.where, style="muted")  # …or the slot, when we can't name it
        return Text("—", style="muted")

    def _channel_subject(self, raw: dict) -> Text:
        """A channel frame's subject: its channel, named by the key its MAC confirms.

        The frame names its channel only by a one-byte fingerprint, which several channels
        can share — so a name is only shown once a key we hold has reproduced the frame's
        own MAC (:func:`~meshterm.core.channels.identify_channel`), never on a fingerprint
        match alone. A channel we hold no key for reads as the fingerprint it advertised,
        under the app's word for a short derived id: ``hash a3``. That is deliberately not
        spelled ``ch a3`` — a message row's ``ch 3`` is a *slot*, and two different numbers
        wearing one prefix in the same column would be worse than saying nothing.
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
        """An addressed frame's subject: the two nodes it travels between.

        Drawn as ``sender → recipient`` on THE hop-sequence widget
        (:mod:`~meshterm.ui.pathline`), so the endpoints of a delivery read exactly as the
        hops of a route do — resolved to contact names, each in its key-derived hue, our
        own node white the moment a frame is addressed to (or from) us. Plain arrows, not
        chips: this is a lane inside a table row, and a chip's padding would cost the very
        cells the names need.

        Both endpoints are named by a single byte of their key — the same one-byte identity
        a relay hop carries, with the same collision odds — so a name here is a good guess,
        not a proof, and the frame's own hashes are always one keypress away in the viewer.
        An anonymous request is the exception in our favour: it carries its sender's *whole*
        key, which resolves exactly, and (unresolved) is shown cut to the same one byte
        as everything else in the lane rather than 64 hex digits nobody can read at a
        glance.

        When both names don't fit the lane the recipient keeps its name and the sender
        drops to its hash: the frame is *addressed*, so the addressee is the half worth
        the cells.
        """
        dest = raw.get("dest_hash") or ""
        src = raw.get("src_key") or raw.get("src_hash") or ""
        if not src:
            return self._token("to", dest)
        named = PathLine([self._hop(src), self._hop(dest)], mode="plain").text()
        if named.cell_len <= self._lanes.subject:
            return named
        # Too wide: the sender drops to its hash, so the addressee keeps its name whole (or
        # as much of it as the lane holds) instead of being the half that gets amputated.
        return PathLine([self._hop(src, named=False), self._hop(dest)], mode="plain").text()

    def _hop(self, value: str, *, named: bool = True) -> PathHop:
        """One endpoint as a path hop: its contact name, or its one-byte hash in that hue.

        Our own node resolves to the app-wide :data:`~meshterm.ui.pathline.SELF_GLYPH`
        rather than its name — the one endpoint the reader never has to be told, and in an
        18-cell lane the cells it gives back are the *other* end's name reading whole
        instead of being cut.

        Args:
            value: The endpoint's key hash (or, for an anonymous request's sender, its
                whole key — shown cut to the lane's one-byte width when it can't be named).
            named: Resolve it at all. ``False`` forces the hash, for the crowded-lane
                fallback that spends the cells on the other end.
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
        """A frame's own token under the one word that says what it is.

        A trace's tag, an ack's checksum, a channel's fingerprint: hex that identifies
        something without being a node's identity, so it takes no palette hue — the
        qualifier recedes into ``faint`` and the value itself stays muted-legible.
        """
        text = Text(f"{word} ", style="faint")
        text.append(value, style="muted")
        return text

    def _entry_scope(self, entry: PacketEntry) -> Scope | None:
        """The scope of a row's frame, or ``None`` where it has none to state.

        Only a raw ``packet`` frame carries the route type and transport codes a scope is
        read from, and only a flood has one; the lane is left blank for everything else —
        a direct frame is never region-filtered, so a word there would claim a meaning it
        lacks. The lane is drawn ``bare``: its heading already says ``SCOPE``, so a known
        region reads as its name alone, while ``unscoped`` and ``unknown scope 3fa1`` stay the
        words they are everywhere (:func:`~meshterm.ui.widgets.scope_text`).
        """
        if entry.kind != "packet" or not isinstance(entry.raw, dict):
            return None
        return self._scope_of(entry.raw)

    def _feed_note(self, entry: PacketEntry) -> Text | None:
        """The row's trailing detail — a message's conversation, and nothing else.

        A relayed frame's route used to sit here, and it never fitted: whatever the fixed
        lanes left it was a handful of cells, so the chain arrived elided to a stub that
        named one hop and hinted at the rest. A route is a shape rather than a lane, and
        it is one keypress away — the packet viewer draws it as a wrapped path line over
        the route graph, with the room to say the whole thing. So the feed says what
        arrived; Enter says how it got here.

        A conversation the subject lane already stood in for is not repeated here: an
        unsigned channel post files itself *under* its channel, and a row that named the
        same conversation twice read as two facts.

        Args:
            entry: The packet the row describes.

        Returns:
            The detail to append, or ``None`` when the class carries none.
        """
        if entry.kind != "message" or not entry.where:
            return None
        if entry.channel is not None and not _channel_sender(entry.text):
            return None  # the subject lane is already the conversation
        return Text(entry.where, style="muted")


async def open_livefeed(ctx: AppContext) -> None:
    """Open the live feed and run it until dismissed.

    Wires the screen to its feeds: stored recent observations seed it, a hub
    subscription streams every new packet in, and a once-a-second ticker keeps the
    clock-sensitive rows honest. Everything is torn down when the screen closes.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..services import trace_runner
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the live feed is only available in the menu")
    session = ctx.ui.session

    contacts = []
    self_name: str | None = None
    channels: list[tuple[str, bytes]] = []
    channel_names: dict[int, str] = {}
    slots_by_idx: dict[int, tuple[str, bytes]] = {}
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            # Through the session cache: contacts and the channel-slot probe are the two
            # slowest reads on a screen open, so reading them once (not per feed open)
            # is what keeps navigation snappy over Bluetooth.
            contacts = await ctx.devstate.contacts()
            self_name = (await ctx.devstate.self_info()).get("name") or None
            slots = await ctx.devstate.channel_slots()
            channels = [(s.name, s.secret) for s in slots]
            channel_names = {s.idx: s.name for s in slots}
            slots_by_idx = {s.idx: (s.name, s.secret) for s in slots}
    except Exception:  # noqa: BLE001 - the feed renders fine without contact names
        contacts = []
    # Contacts first, every name the recorder ever overheard as the fallback — the
    # app-wide rule that a node we can name never renders as a bare hash.
    stored_names = ctx.repo.node_names()
    resolve = trace_runner.make_node_resolver(contacts, stored_names)
    type_of = trace_runner.make_node_type_resolver(contacts)
    key_of = trace_runner.make_name_key_resolver(contacts, stored_names)
    prefix_bytes = await ctx.devstate.routing_prefix_bytes()

    # The cap is the only bound: the newest _FEED_CAP packets ever recorded, however
    # long ago the quiet stretch before them began.
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
        """Repaint once a second so the clock-sensitive rows stay honest."""
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
        except Exception:  # noqa: BLE001 - teardown must never surface a tick hiccup
            pass


def message_scope_reader(
    ctx: AppContext, slots_by_idx: Mapping[int, tuple[str, bytes]]
) -> MessageScopeOf:
    """A reader for a decoded channel message's scope, off its copies in the packet log.

    The radio hands a received message over as text, not as the frame it arrived in, so a
    ``message`` row carries no transport code. Its flooded copies are in the packet log,
    though, and the message paths dialog already finds them by decrypting and matching
    the text (:func:`~meshterm.services.message_paths.channel_arrivals`); every copy
    carries the sender's one code, so their scope is the message's
    (:func:`~meshterm.services.message_paths.message_scope`).

    The copies found are kept per entry — a card repaints every second and the lookup is a
    windowed query plus a decrypt per frame — but only once some were found: a message
    can be told to the feed a beat before its frame is written to the log, and a miss
    cached then would be a miss for good. The region is resolved on every call, against
    the store's memo, so a name learned while the card is open still names it.

    A direct message has no reader: its copies are encrypted end to end and a routed one
    has no scope anyway.

    Args:
        ctx: The application context (its repository and region store).
        slots_by_idx: The device's channels by slot, as ``(name, secret)``.

    Returns:
        The reader, answering ``None`` for anything it can't place.
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
            except Exception:  # noqa: BLE001 - a card without a scope row, never a crash
                return None
            if arrivals:
                found[id(entry)] = arrivals
        return message_scope(arrivals, ctx.region_store.scope_of)

    return read
