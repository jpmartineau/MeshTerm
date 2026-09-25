# SPDX-License-Identifier: Apache-2.0
"""The Message paths dialog: every way one chat message reached this radio.

The interactive face of :mod:`~meshterm.services.message_paths`, opened from the chat
with ``^P`` (or Enter on a picked direct message). The evidence — one logged frame per
arrival, each with the relay path it rode — is laid out twice, so the shape and the
detail read together:

* a **graph** up top draws every distinct path the message took through the shared
  route-graph widget (:mod:`~meshterm.ui.pathgraph`): the currently selected path
  white, the unused paths gray beneath it. The origin sits at the left, we sit at the
  right, and every relay in between gets its own node-type marker (``▲`` repeater, …)
  plus the first byte of its hash — set straight above or below the marker, in the mesh
  name's own colour, so the byte reads as that node and never crowds the line running
  through it. A node-type key sits under the graph. The row list carries the full names,
  so the graph's labels stay two cells wide and a many-path graph stays readable.
* the **arrival list** beneath is two lines per logged copy. On top, alone on its lane
  and introduced by nothing, the **route** as a path line (:mod:`~meshterm.ui.pathline`):
  the origin, every relay, and us — the same two endpoints the graph draws between, so
  the row and the picture start and finish in the same places. Each node is a chip in its
  own hue, named where we know it (``Lakeside``) and standing in its own hash at the
  device's path-hash width where we don't (``e839f2``, keyless grey); our own end is the
  app-wide ``★``. No hash is repeated after a name — the graph above is where the hash
  bytes live, and a chip and its label share the node's colour, so the two halves
  cross-reference by hue instead of by spelling the hex twice. Under it, hanging muted,
  the frame's own facts: time heard, reception SNR, and which resend it was.
  The route is the line you pick — it is what one arrival differs from another by, and
  what the graph highlights. The list scrolls *inside* the dialog, in whatever rows the
  quote and the graph above it leave, with faint ``↑ n more`` / ``↓ n more`` edge markers
  (the app-wide windowed-list pattern, :class:`~meshterm.ui.tui.screen.ListWindow`) — so
  walking the arrivals can never push the picture they belong to out of the box, and the
  fan compresses its own lanes before the list would lose its window. ↑↓ move the
  selection (the graph's highlight follows), PgUp/PgDn page it by a windowful; a
  route longer than the dialog **scrolls horizontally with ←→**, the whole line shifting
  under a cracked chip at whichever edge continues, and snaps back the moment the
  selection moves on. A row you are *not* on is cut the same way — it just cannot slide.

The message's **scope** — the region it was flooded into — rides the title as a status
atom (``Message paths · scope harbour``), stated once for the message rather than per
arrival: every copy carries its sender's transport code unchanged (see
:func:`~meshterm.services.message_paths.message_scope`).

Nothing here transmits; like the service beneath it, this is a read-model over what
the radio already heard.
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
)

if TYPE_CHECKING:
    from ..core.regions import Scope

#: Cells one ←/→ press shifts the selected row by.
_HSTEP = 4

#: Columns the reception-facts line hangs in under the route it belongs to — past the
#: ``❯ `` pointer lane, so the pair reads as one arrival with its detail tucked under it.
_DETAIL_INDENT = 4

#: The fewest rows the fan may compress to, and the most it may spend — the graph gives
#: its lane pitch up to the arrival list before the list would lose its window.
_GRAPH_MIN_ROWS = 5
_GRAPH_MAX_ROWS = 15

#: Lines the arrival list is guaranteed out of the dialog's budget, so a tall fan can
#: never squeeze it below *two* arrivals and the marker saying more are hidden — one row
#: alone is a fact, and what this dialog is for is comparing one arrival against another.
_LIST_MIN_LINES = 5

#: Edge colours: the selected path draws white over the unused paths' gray.
_EDGE_SELECTED = (255, 255, 255)
_EDGE_UNUSED = (110, 110, 110)


class MessagePathsScreen(Screen):
    """A floating dialog: one message's arrivals as a path graph over a row list."""

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
        """Build the dialog over one message's matched arrivals.

        Args:
            message: The chat message whose delivery evidence is shown.
            arrivals: Its logged arrivals, oldest first (may be empty).
            matched: Whether the arrivals were matched by content (a decrypted
                channel frame) rather than merely by time — the summary line warns
                when they weren't.
            resolve: Maps a hop hash to a friendly name when known.
            prefix_bytes: The device's path-hash width — how many bytes of an unnamed
                hop's hash stand in for its (unknown) name.
            self_name: Our own node's name (the graph's right endpoint, white).
            summary: The one-line evidence summary shown under the quoted text.
            source: Display name of the message's origin — the sender parsed from a
                channel message, us for an outbound one, the peer for a direct chat
                (``None`` reads as an unknown ``?`` origin).
            destination: Display name of the node the message was *addressed to* — the far
                end for a message we sent, ourselves for one we received. It closes the
                path line, so an outgoing message reads ``★ ▶ … ▶ Bob`` rather than
                starting and ending on our own star, which is what made every send look
                like a round trip (JP, 2026-09-02). ``None`` closes on our own ``★``.
            type_of: Maps a relay hash to its node type, so the graph marks a repeater
                ``▲`` (etc.) instead of a generic dot; ``None`` keeps the plain dots.
            key_of: Maps the origin's display name back to its node's key, so the
                graph's left endpoint takes its key-derived hue; ``None`` (or an
                unresolvable name) leaves it muted.
            scope: The region the message was flooded into, read off its arrivals'
                frames (:func:`~meshterm.services.message_paths.message_scope`); ``None``
                where no copy was a flood, and the title then states nothing.
            sent_scope: For a message we sent, the line saying what it was flooded under
                (``sent under scope yul``, and why nothing relayed it where that is
                known), drawn under the summary; ``None`` draws nothing.
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
        #: Cells the selected row is shifted left by (reset whenever ↑↓ move), and
        #: how far it *can* shift, measured against the width of the last render.
        self._hshift = 0
        self._hmax = 0
        self._cursor: int | None = None
        #: The arrival list's window over the rows the pinned head leaves (its ``page`` is
        #: the PgUp/PgDn stride), and whether it currently hides any rows — which is what
        #: makes the paging keys worth advertising.
        self._list = ListWindow()
        self._list_hidden = False

    @staticmethod
    def titled(scope: Scope | None) -> str:
        """The dialog's title: its name, then the message's scope as a status atom.

        Every copy of one message carries its sender's transport code (the code is over the
        payload, which no relay touches), so the scope is a fact about the *message*, not
        about any one arrival — stated once, up here, rather than repeated down a list whose
        rows differ only by path. It rides the title because the title is where a screen's
        status atoms chain (``Message paths · scope harbour``) and it costs the body no
        line: on the PicoCalc's 26 rows a line spent here is a lane of the fan or a row of
        the list. The words are :func:`~meshterm.ui.widgets.scope_text`'s, plain — a title
        carries no styling — so ``unscoped`` and ``scoped · 3fa1`` read here exactly as they
        do on the packet viewer's ``route`` row.
        """
        atom = scope_text(scope).plain
        return f"Message paths · {atom}" if atom else "Message paths"

    # --- input ---------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Row keys while there are rows; a bare close hint otherwise.

        The paging atom appears only while the window actually hides arrivals — the
        app-wide rule that a hint never advertises a key that would do nothing.
        """
        if not self._arrivals:
            return "Esc close"
        parts = ["↑↓ move"]
        if self._list_hidden:
            parts.append("PgUp/PgDn scroll")
        parts.extend(("←→ scroll line", "Esc close"))
        return " · ".join(parts)

    @property
    def fkey_lane(self):
        """The shared pager, dimmed while every arrival is already on screen.

        Both banks move the *selection* here — the pager by a windowful, the jumps to the
        first and last arrival — so they are live exactly when there is more than one
        arrival to move between, whether or not the window hides any.
        """
        from .tui.fkeys import default_lane

        return default_lane(nav=len(self._arrivals) > 1)

    def handle(self, action: str, data: str = "") -> None:
        """Move the selection, shift the selected line sideways, or dismiss."""
        rows = len(self._arrivals)
        # Both ends clamp rather than wrap: the arrivals scroll inside a window (see
        # ListWindow), and a highlight that jumped from the last arrival to the first would
        # take the window with it — the one move that looks like the screen changed under you.
        if action == "up" and rows:
            self._index = max(0, self._index - 1)
            self._hshift = 0
        elif action == "down" and rows:
            self._index = min(rows - 1, self._index + 1)
            self._hshift = 0
        elif action == "pageup" and rows:
            # A windowful of *arrivals*, not of body lines: the list is what moves.
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
        """The selected row's body line.

        The window already keeps it inside the dialog, so this only bites on a terminal
        too short for even the compressed head.
        """
        return self._cursor

    # --- rendering -------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """The pinned quote and path graph, then the arrival list windowed under them.

        The head — the quoted message, the evidence summary, the fan and its captions —
        holds still, and only the arrival rows scroll (see
        :class:`~meshterm.ui.tui.screen.ListWindow`), so walking the arrivals can never
        carry the picture they are being compared against out of the dialog. The three
        share one budget: the head takes its lines, the list is guaranteed
        :data:`_LIST_MIN_LINES`, and the fan compresses its lane pitch into whatever is
        left rather than growing past it.
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
            # Wrapped rather than cut: its tail is the reason a message went nowhere.
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
            # One entry per drawn row: a wrapped render_to_ansi is one string holding a
            # newline, which the frame counts as a single line and measures as one.
            lines.extend(render_lines(Text(note, style="muted"), width))
            self._scroll_total = len(lines)
            return lines

        # The rows are drawn before the fan is sized: what they need is one of the terms
        # the fan's row ceiling is struck from.
        blocks = [
            self._row_block(arrival, i == self._index, width)
            for i, arrival in enumerate(self._arrivals)
        ]
        heights = [len(block) for block in blocks]

        # No blank line above the graph: its canvas already opens with air over the
        # topmost lane, so a spacer here would read as two rows of margin.
        revisit = self._revisit_line(width)
        # The fan's own trailing chrome (caption, node-type key, the revisit note) plus the
        # blank that sets the list apart — all struck from the budget before the ceiling is.
        chrome = 2 + len(revisit) + 1
        room = viewport - len(lines) - chrome - min(sum(heights), _LIST_MIN_LINES)
        graph = self._graph_lines(width, max(_GRAPH_MIN_ROWS, min(_GRAPH_MAX_ROWS, room)))
        if graph:
            lines.extend(graph)
            far = "you" if not self._destination else self._destination
            caption = Text(
                f"origin → {far} · white = selected path · labels = hash byte",
                style="faint",
            )
            lines.append(render_to_ansi(caption, width, no_wrap=True))
            lines.extend(render_lines(node_type_legend(width=width), width, no_wrap=True))
            lines.extend(revisit)
        lines.append("")

        # -- the arrival list, windowed into whatever the head left.
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
        """One arrival as the window moves it: its route, then its facts hanging under it."""
        if selected:
            route = self._selected_line(arrival, width)
        else:
            row = Text("  ")
            row.append_text(self._path_text(arrival))
            # Cut here rather than letting the render boundary ellipsize it: an
            # unselected route runs off the lane exactly as the selected one does,
            # and it owes the reader the same cracked chip rather than three dots.
            route = render_to_ansi(cut_to(row, width), width, no_wrap=True)
        detail = Text(" " * _DETAIL_INDENT)
        detail.append_text(self._detail_text(arrival))
        return [route, render_to_ansi(detail, width, no_wrap=True)]

    # -- the rows --

    def _path_text(self, arrival: Arrival) -> Text:
        """One arrival's whole route — the row's first line, and the one you pick.

        The route gets the row's full width to itself, so no ``via`` introduces it: on a
        lane that holds nothing else there is nothing to tell it apart from. It is THE
        path line, so a route here is drawn exactly as a route anywhere else — chips in
        each node's own hue where the terminal can draw them, arrows where it can't.
        Hops read as names, nothing else: an unnamed hop, having no name to show, stands
        in its own hash at the device's path-hash width (muted grey — colour is the
        "this is a name" signal), and no hop repeats its hash after its name. The hash
        bytes are the graph's job, one row up; what ties the two together is the shared
        node hue, not a second spelling of the hex.

        The line runs **origin to us**, not relay to relay (JP, 2026-08-09): a path's
        ends are the nodes it went between, and a chain that opens on its first *relay*
        reads as a route from a node that only passed the message on. So the sender leads
        (:meth:`_origin_hop`) and we close it on the app-wide ``★``, which is exactly what
        the graph one row up draws between — the two now name the same two endpoints
        instead of the row starting a hop later than the picture above it. A hopless
        arrival is no longer a lone word: ``Alice ▶ ★`` *is* what direct delivery looks
        like, and it reads on the same rails as every other row.
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
        """The node the path ends on — where the message was addressed, not always us.

        The line used to close on our own ``★`` unconditionally, which is right for a
        message we *received* and wrong for one we sent: with our star opening the line too
        (:meth:`_origin_hop`), every outgoing message drew ``★ ▶ … ▶ ★`` and read as a round
        trip. It is the same rule the line already follows at its head — a path runs between
        the nodes it went *between* — applied at last to its tail.
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
        """The node the message set out from — the route's true head.

        The same origin the graph's left endpoint draws (:func:`~meshterm.ui.widgets.
        route_graph_style`), named and hued the same way: the sender's display name in its
        key-derived hue, our own ``★`` for a message we sent, and a bare ``?`` where the
        frame named nobody — never a hue guessed from a name we can't place.
        """
        if not self._source:
            return PathHop("?")
        if self._self_name and self._source == self._self_name:
            return PathHop(SELF_GLYPH, you=True)
        return PathHop(self._source, key=self._key_of(self._source) if self._key_of else None)

    def _detail_text(self, arrival: Arrival) -> Text:
        """One arrival's reception facts — when it landed, how loudly, which copy.

        The row's second line, hanging muted under the route it describes: the path is
        what distinguishes one arrival from another (and what the graph above draws), so
        it takes the lane, and the frame's own particulars step in beneath it.

        The hop count leads (:func:`~meshterm.ui.pathline.hops_atom`): it is the one fact
        about the route the line above encodes without stating, and the number two
        arrivals of the same message are compared on before anything else.

        A row stands for a *path*, not a frame (see
        :func:`~meshterm.services.message_paths.collapse`), so a path heard more than once
        says how many times and the time reads as the first of them — a direct send is
        retried and every retry is overheard off every repeater in earshot, which used to
        fill the list with a dozen identical lines. The SNR is the best that path managed,
        which is what it is capable of.
        """
        # What the hop count *is* depends on the route type, so the lane says which: a
        # flooded frame's path is where it has been, a direct-routed one's is where it was
        # going, and a routed frame carrying none has had its route consumed — which is not
        # the same as having arrived in zero hops, however much an empty field looks like it.
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
        """The highlighted path: ``❯`` pointer, shifted by ``←→`` under an edge cut mark.

        The picked line is the route — the one thing here long enough to need scrolling,
        and the one the graph above highlights — and the shift bound is measured here
        against the current width, so a resize can only ever leave the row clamped back
        into range.

        Each edge the route continues past wears :func:`~meshterm.ui.pathline.cut_mark`,
        so a chip the scroll cuts breaks off on a half block in its own colour rather
        than behind an ellipsis: the route is being *slid*, not shortened, and a cracked
        segment is what says so. An arrow-drawn route falls back to the ``…`` the row has
        always shown.
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
        # The marks stand for the cell just outside the window on their own side, so each
        # takes its colour from the nearest cell still drawn — the chip the reader can see
        # being the one that visibly runs on.
        if left_more:
            line.append_text(cut_mark(full, shift + left_more, ELIDE_HEAD))
        line.append_text(crop_cells(full, shift + left_more, inner))
        if right_more:
            line.append_text(cut_mark(full, shift + left_more + inner - 1, ELIDE_TAIL))
        line.style = "cursor"
        return render_to_ansi(line, width, no_wrap=True)

    # -- the graph --

    def _paths(self) -> list[tuple[str, ...]]:
        """The distinct relay paths across the arrivals, in first-heard order."""
        seen: list[tuple[str, ...]] = []
        for arrival in self._arrivals:
            # A frame whose route was consumed has nothing to draw: an empty lane in the fan
            # would be a claim of adjacency the frame never made.
            if arrival.route_known and arrival.hops not in seen:
                seen.append(arrival.hops)
        return seen

    def _graph_lines(self, width: int, max_rows: int) -> list[str]:
        """Draw every distinct path origin → us, the selected one white over gray.

        The shared route-graph widget does the layout (a left-to-right flow of paths that
        diverge and converge, each route its own lane and each relay drawn once); this just
        maps each distinct path to a
        :class:`~meshterm.ui.pathgraph.PathLayer` — the selected one white on top, the
        rest gray beneath — and hands it the shared route-graph callbacks
        (:func:`~meshterm.ui.widgets.route_graph_style`): endpoints named, relays their
        own map marker where the type is known, each labelled by its first hash byte in
        the node's own hue.

        Layout rank is fixed by first-heard order (the first path heard is always the spine),
        exactly as the node detail's Routes tab fixes it by evidence order, so the fan's
        geometry holds still as ↑↓ walk the arrivals — only the emphasis (which lane draws
        white and on top) follows the pick.

        Args:
            width: Canvas width in cells.
            max_rows: The most rows the fan may spend — what the dialog's budget leaves once
                the quote, the fan's own captions and the list's guaranteed lines are paid
                for. A fan with more lanes than that compresses its lane pitch to fit.
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
        """The ⚠ note when any drawn path touches one hop twice, else no line at all.

        These paths are *overheard*, not composed: a repeated hop is real evidence, so the graph
        draws each visit its own marker (``allow_duplicate_nodes``) rather than folding the walk
        into a cycle it cannot seat. That leaves one name on two markers, which this explains —
        once for the whole fan, naming every hop repeated anywhere in it, since the note belongs
        to the picture rather than to whichever arrival ↑↓ happen to be resting on.
        """
        repeated: list[str] = []
        for path in self._paths():
            for hop in revisited_hops(path):
                if hop not in repeated:
                    repeated.append(hop)
        note = revisit_note(repeated, self._resolve, self_name=self._self_name)
        return [] if note is None else [render_to_ansi(note, width, no_wrap=True)]
