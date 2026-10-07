# SPDX-License-Identifier: Apache-2.0
"""The Message paths dialog: all the paths on which one chat message reached our node.

This dialog is the interactive part of :mod:`~meshterm.services.message_paths`. The user
opens it from the chat with ``^P`` (or Enter on a selected direct message). The evidence
is one logged packet for each arrival, each with the relay path that it went through. The
dialog shows this evidence two times, so that the user can read the shape and the detail
together:

* A **graph** at the top draws each different path that the message went through. It
  uses the shared route-graph widget (:mod:`~meshterm.ui.pathgraph`). The selected path
  is white, and the unused paths are grey below it. The origin is at the left, and our
  node is at the right. Each relay between them gets its own node-type marker (``▲``
  repeater, …) and the first byte of its hash. The byte is directly above or below the
  marker, in the colour of the node name. Thus the byte reads as that node, and it stays
  apart from the line that goes through the node. A legend of the node types is below
  the graph. The rows of the list have the full names. Thus the labels of the graph stay
  two cells wide, and a graph with many paths stays readable.
* The **arrival list** below the graph has two lines for each logged copy. The first line
  has only the **route**, as a path line (:mod:`~meshterm.ui.pathline`), with no label
  before it. The route has the origin, each relay, and our node. These are the same two
  ends that the graph draws between, so the row and the picture start and end at the
  same places. Each node is a chip in its own hue. The chip shows the name of the node
  when MeshTerm knows it (``Lakeside``). If not, the chip shows the hash of the node at
  the path-hash width of the device (``e839f2``, in the grey of a node with no key). The
  end at our node is the ``★`` of the whole app. No hash comes after a name, because the
  hash bytes are in the graph above. Also, a chip and its label have the colour of the
  node. Thus the two parts refer to each other by hue, and the hex is not written two
  times. The second line, muted and indented under the route, has the facts of the
  packet: the time when it was heard, the SNR of the reception, and the number of the
  resend.

  The route is the line that the user selects, because the route is the difference
  between one arrival and another, and the graph shows that route in white. The list
  scrolls in the dialog, in the rows that the quote and the graph above it leave. Faint
  ``↑ n more`` and ``↓ n more`` markers are at its edges (the list window of the whole
  app, :class:`~meshterm.ui.tui.screen.ListWindow`). Thus, when the user moves through
  the arrivals, the picture that they belong to never goes out of the box. Also, the
  graph compresses its graph lanes before the list loses its list window. The ↑ and ↓
  keys move the highlight, and the graph follows it. PgUp and PgDn move the highlight by
  one list window. A route that is longer than the dialog **scrolls horizontally with
  ←→**. The full line moves, with a cracked chip at each edge where the route continues.
  When the highlight moves to a different row, the route goes back to its start
  immediately. A row that is not highlighted is cut the same way, but it cannot move.

The **scope** of the message (the region that the message was flooded into) is a status
atom in the title (``Message paths · scope harbour``). The title states it one time for
the message, not for each arrival, because each copy carries the transport code of its
sender unchanged (refer to :func:`~meshterm.services.message_paths.message_scope`).

Nothing in this module transmits. Like the service below it, this dialog is a read-model
over the packets that the radio already heard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text

from ..core.models import ChatMessage
from ..services.message_paths import Arrival
from .pathgraph import PathLayer, render_path_graph, revisited_hops
from .pathline import (
    ELIDE_HEAD,
    ELIDE_TAIL,
    SELF_GLYPH,
    PathHop,
    PathLine,
    cut_mark,
    cut_to,
    hops_atom,
    path_line,
)
from .theme import snr_style
from .tui.render import crop_cells, render_lines, render_to_ansi
from .tui.screen import ListWindow, Screen
from .widgets import (
    NameKeyResolver,
    NodeResolver,
    TypeOf,
    node_type_legend,
    revisit_note,
    route_graph_style,
    scope_text,
    short_frame,
)

if TYPE_CHECKING:
    from ..core.regions import Scope

#: The number of cells by which one ← or → key press moves the selected row.
_HSTEP = 4

#: The indent in cells of the line of reception facts, under the route that it is part
#: of. The indent is past the ``❯ `` pointer lane, so that the two lines read as one
#: arrival with its detail under it.
_DETAIL_INDENT = 4

#: The minimum number of rows that the graph can compress to, and the maximum number of
#: rows that it can use. When rows are not sufficient, the graph makes the pitch of its
#: graph lanes smaller first, so that the arrival list keeps its list window.
_GRAPH_MIN_ROWS = 5
_GRAPH_MAX_ROWS = 15

#: The number of lines that the arrival list always gets from the budget of the dialog.
#: Thus a tall graph can never make the list smaller than two arrivals and the marker
#: that says that more are hidden. One row alone is only a fact, but the purpose of this
#: dialog is to compare one arrival with another.
_LIST_MIN_LINES = 5

#: The colours of the edges: the selected path is white, over the grey of the unused paths.
_EDGE_SELECTED = (255, 255, 255)
_EDGE_UNUSED = (110, 110, 110)


class MessagePathsScreen(Screen):
    """A floating dialog: the arrivals of one message as a path graph above a list of rows."""

    def __init__(
        self,
        message: ChatMessage,
        arrivals: list[Arrival],
        *,
        matched: bool,
        resolve: NodeResolver,
        prefix_bytes: int,
        self_name: str | None,
        summary: str,
        source: str | None = None,
        destination: str | None = None,
        type_of: TypeOf | None = None,
        key_of: NameKeyResolver | None = None,
        scope: Scope | None = None,
        sent_scope: Text | None = None,
    ) -> None:
        """Build the dialog for the matched arrivals of one message.

        Args:
            message: The chat message whose evidence of delivery the dialog shows.
            arrivals: The logged arrivals of the message, oldest first. The list can be
                empty.
            matched: Whether the arrivals were matched by content (a decrypted channel
                packet), and not only by time. When they were not, the summary line
                shows a warning.
            resolve: Gives the friendly name for the hash of a hop, when the name is
                known.
            prefix_bytes: The path-hash width of the device: the number of bytes of the
                hash of a hop with no name that the dialog shows instead of its unknown
                name.
            self_name: The name of our node (the right end of the graph, in white).
            summary: The one-line summary of the evidence, shown under the quoted text.
            source: The display name of the origin of the message: the sender parsed
                from a channel message, our node for an outbound message, or the peer
                for a direct chat. ``None`` shows as an unknown origin, ``?``.
            destination: The display name of the node that the message was addressed
                to: the far end for a message that our node sent, or our node for a
                message that it received. This node ends the path line. Thus an
                outgoing message reads ``★ ▶ … ▶ Bob``, and it does not start and end
                on our own star. Before, it did, and each send looked like a round trip
                (JP, 2026-09-02). ``None`` ends the line on our own ``★``.
            type_of: Gives the node type for the hash of a relay. Thus the graph marks
                a repeater with ``▲`` (and the other types with their markers), not with
                a generic dot. ``None`` keeps the plain dots.
            key_of: Gives the key of the node for the display name of the origin. Thus
                the left end of the graph gets the hue that comes from the key. ``None``
                (or a name that it cannot resolve) leaves that end muted.
            scope: The region that the message was flooded into, read from the packets
                of its arrivals (:func:`~meshterm.services.message_paths.message_scope`).
                It is ``None`` when no copy was a flood. Then the title states no scope.
            sent_scope: For a message that our node sent: the line that says the scope
                that the message was flooded under (``sent under scope yul``), and why
                no node relayed it, when that is known. The dialog draws this line under
                the summary. ``None`` draws nothing.
        """
        super().__init__()
        self.title = self.titled(scope)
        self._message = message
        self._arrivals = arrivals
        self._matched = matched
        self._resolve = resolve
        self._prefix_bytes = prefix_bytes
        self._self_name = self_name
        self._summary = summary
        self._source = source
        self._destination = destination
        self._type_of = type_of
        self._key_of = key_of
        self._sent_scope = sent_scope
        self._index = 0
        #: The number of cells by which the selected row is moved left (set to zero each
        #: time ↑ or ↓ moves the highlight), and the maximum move, measured against the
        #: width of the last paint.
        self._hshift = 0
        self._hmax = 0
        self._cursor: int | None = None
        #: The list window of the arrival list, over the rows that the pinned head leaves
        #: (its ``page`` is the step of PgUp and PgDn). Also, whether the list window hides
        #: rows now.
        #: The hint shows the paging keys only when it does, because only then do these
        #: keys do something.
        self._list = ListWindow()
        self._list_hidden = False

    @staticmethod
    def titled(scope: Scope | None) -> str:
        """The title of the dialog: its name, then the scope of the message as a status atom.

        Each copy of one message carries the transport code of its sender. (The code is
        calculated over the payload, and no relay changes the payload.) Thus the scope is a
        fact about the message, not about one arrival. The title states it one time, and
        the list does not repeat it on rows that are different only by path. The scope is
        in the title, because the status atoms of a screen make a chain in the title
        (``Message paths · scope harbour``). Also, there it uses no line of the body. On
        the 26 rows of the PicoCalc, a line here is one graph lane or one row of the
        list. The words come from :func:`~meshterm.ui.widgets.scope_text`, without style,
        because a title has no styles. Thus ``unscoped`` and ``unknown scope 3fa1`` read
        here the same as on the ``route`` row of the packet viewer.
        """
        atom = scope_text(scope).plain
        return f"Message paths · {atom}" if atom else "Message paths"

    # --- input ---------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The keys for the rows while there are rows. If there are no rows, only the close hint.

        The paging atom shows only while the list window hides arrivals. This is the rule of
        the whole app: a hint never shows a key that does nothing.
        """
        if not self._arrivals:
            return "Esc close"
        parts = ["↑↓ move"]
        if self._list_hidden:
            parts.append("PgUp/PgDn scroll")
        parts.extend(("←→ scroll line", "Esc close"))
        return " · ".join(parts)

    @property
    def picocalc_lyra_lane(self):
        """The shared pager, dim when there is only one arrival or none.

        Here, both banks move the highlight. The pager moves it by one list window, and
        the jumps move it to the first and the last arrival. Thus the chips are live
        exactly when there is more than one arrival to move between, also when the list
        window hides no arrivals.
        """
        from .tui.fkeys import default_lane

        return default_lane(nav=len(self._arrivals) > 1)

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight, move the selected line to the side, or close the dialog."""
        rows = len(self._arrivals)
        # Both ends clamp. The highlight does not go around to the other end, because the
        # arrivals scroll in a list window (refer to ListWindow). If the highlight jumps
        # from the last arrival to the first, the list window goes with it. That is the only
        # move that looks like a change of the whole screen.
        if action == "up" and rows:
            self._index = max(0, self._index - 1)
            self._hshift = 0
        elif action == "down" and rows:
            self._index = min(rows - 1, self._index + 1)
            self._hshift = 0
        elif action == "pageup" and rows:
            # One list window of arrivals, not of body lines, because the list is what moves.
            self._index = max(0, self._index - self._list.page)
            self._hshift = 0
        elif action in ("pagedown", "space") and rows:
            self._index = min(rows - 1, self._index + self._list.page)
            self._hshift = 0
        elif action in ("home", "ctrl_home") and rows:
            self._index = 0
            self._hshift = 0
        elif action in ("end", "ctrl_end") and rows:
            self._index = rows - 1
            self._hshift = 0
        elif action == "left":
            self._hshift = max(0, self._hshift - _HSTEP)
        elif action == "right":
            self._hshift = min(self._hmax, self._hshift + _HSTEP)
        elif action in ("escape", "enter"):
            self.resolve(None)

    def cursor_line(self) -> int | None:
        """The body line of the selected row.

        The list window already keeps the row in the dialog. Thus this value has an effect only
        on a terminal that is too short for the compressed head.
        """
        return self._cursor

    # --- rendering -------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """The pinned quote and path graph, then the arrival list in a list window below them.

        The head does not move. It is the quoted message, the summary of the evidence, and
        the graph with its captions. Only the arrival rows scroll (refer to
        :class:`~meshterm.ui.tui.screen.ListWindow`). Thus, when the user moves through the
        arrivals, the picture that they are compared with never goes out of the dialog.
        The three parts share one budget. The head gets its lines, and the list always gets
        :data:`_LIST_MIN_LINES`. The graph compresses the pitch of its graph lanes into the
        rows that remain, and it does not grow past them.
        """
        viewport = self._scroll_viewport
        quoted = self._message.text.replace("\n", " ")
        if len(quoted) > 64:
            quoted = quoted[:63] + "…"
        stamp = Text(self._message.created_at.astimezone().strftime("%b %d %H:%M"), style="muted")
        stamp.append("  ·  ", style="muted")
        stamp.append(self._summary, style="muted" if self._matched else "warn")

        lines = [
            render_to_ansi(Text(f"“{quoted}”"), width, no_wrap=True),
            render_to_ansi(stamp, width, no_wrap=True),
        ]
        if self._sent_scope is not None:
            # Wrapped, not cut, because its end is the reason why a message went nowhere.
            lines.extend(render_lines(self._sent_scope, width))
        self._cursor = None
        self._list_hidden = False
        if not self._arrivals:
            lines.append("")
            note = (
                "Nothing overheard — the radio only logs frames it hears while "
                "MeshTerm is listening."
                if self._matched
                else "No direct-message frames logged in the window."
            )
            # One entry for each drawn row. A wrapped render_to_ansi is one string that
            # holds a newline, and the frame counts it and measures it as one line.
            lines.extend(render_lines(Text(note, style="muted"), width))
            self._scroll_total = len(lines)
            return lines

        # The rows are drawn before the graph gets its size, because the lines that the
        # rows need are one of the terms that MeshTerm subtracts to find the row limit of
        # the graph.
        blocks = [
            self._row_block(arrival, i == self._index, width)
            for i, arrival in enumerate(self._arrivals)
        ]
        heights = [len(block) for block in blocks]

        # No blank line above the graph. Its canvas already starts with empty space above
        # the top graph lane, so a blank line here reads as two rows of margin.
        revisit = self._revisit_line(width)
        # The caption and the node-type legend of the graph. A short frame leaves them out,
        # because the markers have one meaning in the whole app, and the rows go to the
        # list (refer to widgets.short_frame).
        key: list[str] = []
        if not short_frame(viewport):
            far = "you" if not self._destination else self._destination
            caption = Text(
                f"origin → {far} · white = selected path · labels = hash byte",
                style="faint",
            )
            key.append(render_to_ansi(caption, width, no_wrap=True))
            key.extend(render_lines(node_type_legend(width=width), width, no_wrap=True))
        # The chrome after the graph (that legend and the revisit note), and the blank line
        # that puts the list apart. MeshTerm subtracts all of them from the budget before it
        # calculates the row limit.
        chrome = len(key) + len(revisit) + 1
        room = viewport - len(lines) - chrome - min(sum(heights), _LIST_MIN_LINES)
        graph = self._graph_lines(width, max(_GRAPH_MIN_ROWS, min(_GRAPH_MAX_ROWS, room)))
        if graph:
            lines.extend(graph)
            lines.extend(key)
            lines.extend(revisit)
        lines.append("")

        # -- the arrival list, in a list window in the rows that the head left.
        window = max(min(heights), viewport - len(lines))
        top, count = self._list.fit_blocks(heights, window, self._index)
        if top > 0:
            lines.append(render_to_ansi(ListWindow.marker(top, "above"), width))
        for i in range(top, top + count):
            if i == self._index:
                self._cursor = len(lines)
            lines.extend(blocks[i])
        below = len(blocks) - top - count
        if below > 0:
            lines.append(render_to_ansi(ListWindow.marker(below, "below"), width))
        self._list_hidden = top > 0 or below > 0
        self._scroll_total = len(lines)
        return lines

    def _row_block(self, arrival: Arrival, selected: bool, width: int) -> list[str]:
        """One arrival as the list window moves it: its route, then its facts indented under it."""
        if selected:
            route = self._selected_line(arrival, width)
        else:
            row = Text("  ")
            row.append_text(self._path_text(arrival))
            # Cut the route here, and do not let the render boundary ellipsize it. An
            # unselected route goes past the end of the lane the same as the selected
            # route, and it must show the user the same cracked chip, not three dots.
            route = render_to_ansi(cut_to(row, width), width, no_wrap=True)
        detail = Text(" " * _DETAIL_INDENT)
        detail.append_text(self._detail_text(arrival))
        return [route, render_to_ansi(detail, width, no_wrap=True)]

    # -- the rows --

    def _path_text(self, arrival: Arrival) -> Text:
        """The full route of one arrival: the first line of the row, which the user selects.

        The route has the full width of the row, so no ``via`` comes before it. On a lane
        that holds nothing else, there is nothing to tell it apart from. It is the only
        path line of the app, so a route here is drawn the same as a route at all other
        places: chips in the hue of each node where the terminal can draw them, and arrows
        where it cannot. The hops show only names. A hop with no name to show shows its
        own hash at the path-hash width of the device instead (in muted grey, because
        colour is the signal for "this is a name"). No hop repeats its hash after its
        name. The hash bytes are in the graph, one row up. The node hue that the two share
        connects them, not a second copy of the hex.

        The line goes **from the origin to our node**, not from relay to relay (JP,
        2026-08-09). The ends of a path are the nodes that it went between. A chain that
        starts on its first relay reads as a route from a node that only passed the
        message on. Thus the sender is first (:meth:`_origin_hop`), and the line ends on
        the ``★`` of the whole app. The graph one row up draws between these same two
        ends. Now the two name the same two ends, and the row does not start one hop
        after the picture above it. An arrival with no hops is not a single word now:
        ``Alice ▶ ★`` is how direct delivery looks, and it has the same form as all the
        other rows.
        """
        relays = path_line(
            arrival.hops,
            self._resolve,
            prefix_bytes=self._prefix_bytes,
            self_name=self._self_name,
            hash_as_name=True,
        ).hops
        return PathLine([self._origin_hop(), *relays, self._far_hop()]).text()

    def _far_hop(self) -> PathHop:
        """The node at the end of the path: the addressee of the message, not always our node.

        Before, the line always ended on the ``★`` of our node. That is correct for a
        message that our node received, and wrong for a message that our node sent. Our
        star also starts the line (:meth:`_origin_hop`), so each outgoing message drew
        ``★ ▶ … ▶ ★`` and read as a round trip. This is the same rule that the line already
        follows at its start (a path goes between the nodes that it went between), now
        also at its end.
        """
        if not self._destination:
            return PathHop(SELF_GLYPH, you=True)
        if self._self_name and self._destination == self._self_name:
            return PathHop(SELF_GLYPH, you=True)
        return PathHop(
            self._destination,
            key=self._key_of(self._destination) if self._key_of else None,
        )

    def _origin_hop(self) -> PathHop:
        """The node that the message started from: the true start of the route.

        It is the same origin that the left end of the graph draws
        (:func:`~meshterm.ui.widgets.route_graph_style`), with the same name and hue. The
        display name of the sender is in the hue that comes from its key. A message that
        our node sent shows our own ``★``. When the packet named no node, the hop is only
        ``?``. MeshTerm never guesses a hue from a name that it cannot place.
        """
        if not self._source:
            return PathHop("?")
        if self._self_name and self._source == self._self_name:
            return PathHop(SELF_GLYPH, you=True)
        return PathHop(self._source, key=self._key_of(self._source) if self._key_of else None)

    def _detail_text(self, arrival: Arrival) -> Text:
        """The reception facts of one arrival: when it arrived, how strong it was, which copy.

        This is the second line of the row, muted and indented under the route that it
        describes. The path is the difference between one arrival and another (and the
        graph above draws it), so the path gets the lane. The details of the packet go
        below it.

        The hop count is first (:func:`~meshterm.ui.pathline.hops_atom`). It is the one
        fact about the route that the line above encodes but does not state. Also, it is
        the first number by which the user compares two arrivals of the same message.

        A row is one path, not one packet (refer to
        :func:`~meshterm.services.message_paths.collapse`). Thus a path that was heard more
        than one time shows how many times, and the time is the time of the first of them.
        The reason is that a direct send is tried again, and the device overhears each try
        from each repeater in range. Before, this filled the list with a dozen lines that
        were the same. The SNR is the best SNR of that path, because that shows what the
        path can do.
        """
        # The meaning of the hop count depends on the route type, so the lane says which
        # type. The path of a flooded packet is where it went. The path of a direct-routed
        # packet is where it was going to go. A routed packet that carries no path had its
        # route consumed. That is not the same as an arrival in zero hops, although the
        # empty field looks the same.
        if not arrival.route_known:
            text = Text("routed", style="warn")
            text.append("  ·  route not carried", style="muted")
        else:
            text = hops_atom(len(arrival.hops))
            if arrival.routed:
                text.append("  routed", style="muted")
        text.append("  ")
        text.append(arrival.when.astimezone().strftime("%H:%M:%S"), style="muted")
        if arrival.copies > 1:
            text.append(f"  ×{arrival.copies}", style="muted")
        if arrival.snr is not None:
            text.append("  ")
            text.append(f"{arrival.snr:+.1f} dB", style=snr_style(arrival.snr))
        if arrival.resend:
            text.append(f"  ·  resend #{arrival.resend}", style="muted")
        return text

    def _selected_line(self, arrival: Arrival, width: int) -> str:
        """The highlighted path: the ``❯`` pointer, and the route moved by ``←→`` under cut marks.

        The selected line is the route. It is the only thing here that can be long enough
        to scroll, and it is the path that the graph above shows in white. The limit of the
        move is measured here against the current width. Thus, after a resize, the row can
        only be clamped back into its range.

        Each edge past which the route continues gets
        :func:`~meshterm.ui.pathline.cut_mark`. Thus a chip that the scroll cuts breaks off
        on a half block in its own colour, not behind an ellipsis. The route moves to the
        side, but it is not made shorter, and the cracked segment shows this. A route drawn
        with arrows uses the ``…`` that the row always showed.
        """
        full = self._path_text(arrival)
        avail = max(1, width - 2)
        self._hmax = max(0, full.cell_len - avail)
        self._hshift = min(self._hshift, self._hmax)
        shift = self._hshift
        left_more = 1 if shift > 0 else 0
        right_more = 1 if shift + avail < full.cell_len else 0
        inner = max(1, avail - left_more - right_more)
        line = Text("❯ ", style="cursor", no_wrap=True)
        # Each mark stands for the cell immediately outside the visible part on its side. Thus
        # each mark takes its colour from the nearest cell that is still drawn, because the
        # chip that the user can see is the chip that visibly continues.
        if left_more:
            line.append_text(cut_mark(full, shift + left_more, ELIDE_HEAD))
        line.append_text(crop_cells(full, shift + left_more, inner))
        if right_more:
            line.append_text(cut_mark(full, shift + left_more + inner - 1, ELIDE_TAIL))
        line.style = "cursor"
        return render_to_ansi(line, width, no_wrap=True)

    # -- the graph --

    def _paths(self) -> list[tuple[str, ...]]:
        """The different relay paths of the arrivals, in the order that they were first heard."""
        seen: list[tuple[str, ...]] = []
        for arrival in self._arrivals:
            # A packet whose route was consumed has nothing to draw. An empty graph lane says
            # that the two ends are next to each other, but the packet never said that.
            if arrival.route_known and arrival.hops not in seen:
                seen.append(arrival.hops)
        return seen

    def _graph_lines(self, width: int, max_rows: int) -> list[str]:
        """Draw each different path from the origin to our node, the selected path white over grey.

        The shared route-graph widget does the layout: a flow of paths from left to right
        that go apart and come together, with each route on its own graph lane and each
        relay drawn one time. This method only maps each different path to a
        :class:`~meshterm.ui.pathgraph.PathLayer` (the selected path white on top, the
        other paths grey below it). Then it gives the widget the shared route-graph
        callbacks (:func:`~meshterm.ui.widgets.route_graph_style`). These callbacks name
        the ends, give each relay its own map marker when the type is known, and label each
        relay with the first byte of its hash in the hue of the node.

        The order in which the paths were first heard sets the layout rank (the first path
        heard is always the spine). The Routes tab of the node detail sets the rank by the
        order of the evidence in the same way. Thus the shape of the graph does not move
        while ↑ and ↓ move through the arrivals. Only the emphasis (which graph lane is
        white and on top) follows the selection.

        Args:
            width: The width of the canvas in cells.
            max_rows: The maximum number of rows for the graph. This is what the budget of
                the dialog leaves after the quote, the captions of the graph, and the
                minimum lines of the list. A graph with more graph lanes than that
                compresses the pitch of its graph lanes to fit.
        """
        selected = self._arrivals[self._index].hops
        paths = self._paths()
        if not paths:
            return []
        layers = [
            PathLayer(
                hops=path,
                color=_EDGE_SELECTED if path == selected else _EDGE_UNUSED,
                priority=len(paths) - i,
                emphasis=1 if path == selected else 0,
            )
            for i, path in enumerate(paths)
        ]
        glyph_of, label_of, label_rgb_of = route_graph_style(
            resolve=self._resolve,
            self_name=self._self_name,
            source=self._source,
            destination=self._destination,
            type_of=self._type_of,
            key_of=self._key_of,
        )
        return render_path_graph(
            layers,
            width,
            max_rows=max_rows,
            glyph_of=glyph_of,
            label_of=label_of,
            label_rgb_of=label_rgb_of,
            allow_duplicate_nodes=True,
        )

    def _revisit_line(self, width: int) -> list[str]:
        """The ⚠ note when a drawn path goes through one hop two times. If not, no line.

        These paths were overheard, not composed. Thus a repeated hop is real evidence, and
        the graph draws a marker for each visit (``allow_duplicate_nodes``). It does not fold
        the walk into a cycle that it cannot place. The result is one name on two markers,
        and this note explains it. The note is one time for the whole graph, and it names
        each hop that is repeated at any place in the graph. The reason is that the note is
        about the picture, not about the arrival that ↑ and ↓ selected at that time.
        """
        repeated: list[str] = []
        for path in self._paths():
            for hop in revisited_hops(path):
                if hop not in repeated:
                    repeated.append(hop)
        note = revisit_note(repeated, self._resolve, self_name=self._self_name)
        return [] if note is None else [render_to_ansi(note, width, no_wrap=True)]
