# SPDX-License-Identifier: Apache-2.0
"""The trace path composer: build a forced path hop by hop, with help from observed links.

A floating dialog over a live trace screen. The route that the user builds goes across
the top (``★ → hop → … → ★``). Below it is a list of suggestions: the nodes that the
current tail of the path can hear, as the topology evidence shows, strongest observed
link first (refer to :meth:`~meshterm.services.topology.MeshTopology.next_hops`). Each
link is evidence for the two directions. Thus, if MeshTerm ever received a path through
two nodes, the composer offers that link in each direction.

The composer serves the two trace features. The feature that owns it decides how the
return half of the route is written. (The trace protocol has no separate field for the
return path. The spec that MeshTerm sends to the device is one walk that ends where our
node can hear it.)

* **Target mode** ("Trace target", a ``target_id`` is pinned): the user composes only
  the outbound leg. The target stays pinned as the turning point, and the return is the
  outbound hops in the mirrored order. The preview resolves that half and dims it,
  because the user does not choose it. An "Auto" action lets the device do the routing
  again.
* **Path mode** ("Trace path", no target): the user composes the full walk, hop by hop,
  until the route comes back to where our node can hear it. The composer fills in (and
  dims) only the last step back to our node. The preview starts as ``★ → + → ★`` only.
  There is no "Auto" action, because with no destination, the device has nothing to
  route to.

Interaction, which follows the pattern of the reorder screen (a highlight that moves over
rows and actions):

* ↑/↓ move over the suggestions and the action rows. Enter on a suggestion inserts it at
  the insertion slot of the route.
* ←/→ move that insertion slot along the editable leg. The slot is in the route as a hop
  of its own: the ``+`` slot in the ``cursor`` white, where the next selected node goes.
  Thus the user can insert a hop (at the slot) or remove a hop (⌫, the hop to its left)
  at each position in the route, not only at the end. The slot starts at the end of the
  composed leg, where an insert is an append. That is the classic flow, with no change.
  The list of suggestions follows the slot: it always offers next hops from the node to
  the left of the slot.
* Typed text filters the suggestions by name or by hash. When the typed text is hex with
  an even length, a row appears that adds it as a custom hop. Thus the user can force a
  node that MeshTerm never observed (or a bare hash from another tool) into the route.
* Backspace (⌫) removes the filter text first. When the filter is empty, it removes the
  hop to the left of the slot.
* When the slot is after a repeater for which we have admin credentials, a "Fetch
  neighbours" row asks that repeater, over the mesh, for its own neighbour table. That
  gives new evidence from a second point on the mesh, at the exact place where the
  composer has no more evidence. The dialog resolves with :class:`FetchNeighbours`. Then
  the owning flow gets the table, refreshes the topology, and opens the composer again in
  the same state (the hops and the slot stay the same).
* Enter on **Use this path** returns the composed spec. Esc cancels with no change.
* A walk that goes over a link two times in the same direction shows a yellow ⚠ note
  below the preview. A trace can still walk it, but it is no longer a *trail*, so the
  trophy case will ignore it (the no-cheat rule, refer to
  :mod:`~meshterm.services.records`).

Nodes render through the shared path widget, in the trace format: ``Name (hash)``, with
the names in their hues (the same in all the app), and the hash at the path-hash width
that the session chose. When the topology cannot name a hop, the composer uses the
resolver of the owning screen (refer to :meth:`PathComposerScreen._resolve_entry`). Thus
a node looks the same here as in the trace screen below this dialog. The route preview is
that same route lane of the trace screen, live: :mod:`~meshterm.ui.pathline` chips (or
arrows), wrapped at hop boundaries, our two ends the bare ``★``, and no hash after a
name. That preview is the path, so the "Use this path" row does not spell it again: the
row names the action, and the picture above it names the route. The list of suggestions
scrolls in a list window below the pinned route preview (faint ``↑/↓ n more`` markers at
its edges, and PgUp/PgDn move by one full list window). Thus the route that the user
builds never leaves the screen.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text

from ..services.records import first_repeated_edge
from ..services.topology import (
    HopSuggestion,
    Link,
    MeshTopology,
    is_path_hash,
    render_custom_spec,
    render_forced_spec,
)
from .pathline import PathLine, cut_to, path_line
from .theme import snr_style
from .tui.render import query_line, render_lines, render_to_ansi
from .tui.screen import ListWindow, Screen
from .widgets import NodeResolver, age_seconds, format_age, identity_label, path_text

#: The sentinel spec for "no forced path, let the device route" (the convention of the
#: trace screen for an empty spec). It has a meaning only in target mode. A path walk has
#: no target for the device to route to, so path mode never resolves with it.
AUTO_SPEC = ""

#: The maximum number of suggestions to show. After this number, the evidence is too weak
#: to be important, and the dialog would become larger than the screen.
_MAX_SUGGESTIONS = 12

#: The sentinels of the action rows (different from the suggestion rows, which hold node
#: ids). There is no cancel sentinel: Esc is the way to leave. A row that only did what
#: Esc does cost one more highlight stop above the "Use this path" row.
_USE = "use"
_AUTO = "auto"

#: One-letter tags for the classes of evidence behind a link, shown beside each
#: suggestion: T(race), R(oute: the out_path that the firmware learned), P(acket log),
#: N(eighbour table that MeshTerm got from a repeater).
_SOURCE_TAGS = {"trace": "T", "route": "R", "packet": "P", "neighbour": "N"}

#: The inline no-cheat warning. It shows below the route preview while the composed walk
#: goes over a link two times in the same direction. If a walk repeats a section, the
#: score of that section can be counted many times. Thus the trophy case disqualifies
#: such a walk: it is no longer a *trail* (in graph theory, a walk with no repeated edge,
#: refer to :func:`~meshterm.services.records.first_repeated_edge`). The user can still
#: compose such a walk. The warning says only that the walk cannot set records.
_NOT_A_TRAIL = "⚠ repeats a link — not a trail, so records ignore this walk"


@dataclass(frozen=True, slots=True)
class FetchNeighbours:
    """The result of the composer when the user asks a repeater for its neighbours.

    The dialog itself never uses the radio. It resolves with this marker. Then the owning
    flow does the login and gets the neighbours, refreshes the topology, and opens the
    composer again, with the values of :attr:`PathComposerScreen.hops` and
    :attr:`PathComposerScreen.cursor` from before.

    Attributes:
        node: The canonical id of the repeater to ask (the anchor of the insertion slot
            when the user selected the row).
    """

    node: str


class PathComposerScreen(Screen):
    """Compose a forced trace path step by step: mirrored at a target, or routed by hand.

    The screen resolves with one of these values:

    - The finished spec, as comma-separated hex. In target mode, it is the outbound hops,
      the target, then the mirrored return. In path mode, it is the composed walk exactly.
    - :data:`AUTO_SPEC`, to let the device route (target mode only).
    - :data:`~meshterm.ui.tui.screen.CANCEL`.

    The dialog can only become larger (refer to
    :attr:`~meshterm.ui.tui.screen.Screen.grow_only`). Each added hop makes the route
    preview wider, and each new tail brings a different set of suggestions. Thus a box that
    fits its content would change size at each key press. Instead, the box keeps its largest
    height and width while the user composes, like the packet viewer.
    """

    grow_only = True

    #: The keyboard keys of the composer, in the order of the hint rules: navigation, then
    #: actions, then Esc. Only the verb of Esc changes (refer to :attr:`footer_hint`).
    #: ``←→ slot``, not ``←→ cursor``: in this app, the word "cursor" names the row that ❯
    #: points to (the highlight), and ↑↓ move it here. The arrows ←→ move the **insertion
    #: slot** along the path that the user builds. "Insertion slot" is the app's own word
    #: for it. It is also the only thing, other than the highlight, that has the selection
    #: white.
    _HINT = "↑↓ move · ←→ slot · Enter add · type to filter · ⌫ remove · Esc cancel"

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The keyboard keys of the composer, with ``Esc clear`` while there is a typed entry.

        Esc clears the typed entry before it cancels the composer, exactly as ⌫ removes the
        entry before it removes a hop. Thus the line says which of the two actions the key
        press will do. This is the filter rule for all the app: the map, the mesh walk, and
        each select list show the same.
        """
        if not self._entry:
            return self._HINT
        return self._HINT.replace("Esc cancel", "Esc clear")

    def __init__(
        self,
        *,
        device_label: str,
        topology: MeshTopology,
        width_bytes: int,
        target_id: str | None = None,
        target_hash: str | None = None,
        target_label: str | None = None,
        device_hash: str | None = None,
        hops: list[str] | None = None,
        cursor: int | None = None,
        fetch_nodes: frozenset[str] = frozenset(),
        resolve: NodeResolver = identity_label,
    ) -> None:
        """Build the composer.

        A pinned target (all three ``target_*`` arguments) selects target mode. The
        suggestions do not include the target (the path turns at it implicitly), and the
        returned spec adds the target and the mirrored return at its end. With no target,
        the composer is in path mode: the user composes the full walk by hand, and the
        composer returns it exactly.

        Args:
            device_label: The name of our node, at the start and at the end of the route
                preview.
            topology: The evidence graph that the suggestions come from.
            width_bytes: The preferred path-hash width for each hop (bytes) of the
                returned spec.
            target_id: The canonical id of the pinned target, or ``None`` for path mode.
            target_hash: The key prefix of the target, in hex. It is the turning point of
                the symmetric spec. When the user confirms the path, it is truncated to the
                width of the spec.
            target_label: The name of the target to show in the route preview.
            device_hash: The public key of our node. Thus the endpoints of the preview have
                a hash, exactly like the route line of the trace screen.
            hops: The canonical ids of the hops that are already composed (when the
                dialog opens again, it continues where the user stopped).
            cursor: The arrow index where the insertion slot starts again (when the
                dialog opens again after a neighbour request). ``None`` puts the slot at
                the end of the path, the position for an append.
            fetch_nodes: The canonical ids of the nodes whose live neighbour table
                MeshTerm can get (repeater contacts with a public key). When the tail of
                the path is one of them, the "Fetch neighbours" row appears.
            resolve: The node resolver of the owning screen. It is the fallback that
                names a hop that the topology cannot name (refer to
                :meth:`_resolve_entry`). Thus the composer shows the same names as the
                screen that opened it.
        """
        super().__init__()
        self.title = f"Compose path — {target_label}" if target_label else "Compose path"
        self._target_id = target_id
        self._target_hash = (target_hash or "").lower().removeprefix("0x")
        self._target_label = target_label
        self._device_label = device_label
        self._device_hash = (device_hash or "").lower().removeprefix("0x")
        self._topology = topology
        self._resolve = resolve
        #: The resolved names to show, by node id (refer to :meth:`_resolve_entry`).
        self._names: dict[str, str] = {}
        #: The graph ids that are the same node as each node (refer to :meth:`_aliases`).
        self._alias_cache: dict[str, set[str]] = {}
        self._width_bytes = width_bytes
        self._hops: list[str] = list(hops or [])
        #: The insertion slot: the joining arrow of the editable leg where the slot is.
        #: Arrow k is between shown node k (our node at 0, or else hop k) and the node
        #: after it. An insert goes to hops index k, and ⌫ removes hop k-1.
        self._cursor = len(self._hops) if cursor is None else max(0, min(cursor, len(self._hops)))
        self._fetch_nodes = fetch_nodes
        self._entry = ""
        self._index = 0
        #: The list window of the suggestion and action rows, below the pinned route preview.
        self._list = ListWindow()

    @property
    def hops(self) -> list[str]:
        """The hops that the user composed until now (to restore after a neighbour request)."""
        return list(self._hops)

    @property
    def cursor(self) -> int:
        """The arrow index of the insertion slot (to restore after a neighbour request)."""
        return self._cursor

    # --- state -----------------------------------------------------------------

    @property
    def _mirrored(self) -> bool:
        """Whether the return leg is a mirror at the pinned target (target mode)."""
        return self._target_id is not None

    def _anchor(self) -> str:
        """The node to the left of the insertion slot (our node when the slot is at the start).

        The suggestions, the "Fetch neighbours" row, and the "Next hop from" heading all
        follow this anchor. An insert continues the walk from here, at each position of the
        slot.
        """
        return self._hops[self._cursor - 1] if self._cursor else self._topology.self_id

    def _same_node(self, a: str, b: str) -> bool:
        """Whether two graph ids are one node: the same name, and one id a prefix of the other.

        The evidence graph keeps a hop hash that it cannot identify as a vertex of its own.
        A 1-byte ``3d`` that matches the prefix of two contacts stays ``3d``. It is
        separate from the ``3d63c6429436`` that the wider observations of the node went
        to. The two ids are real, with real evidence, and the graph is correct not to
        guess. But on the screen, after the name resolver names them (refer to
        :meth:`_resolve_entry`), they are one row, shown two times. Thus the test has two
        parts on purpose: a shared name, and one hash that is a prefix of the other. Two
        hashes with no name never merge, because each resolves to itself, so the names
        are different. Two nodes with the same name and unrelated keys (a duplicate
        contact name, a renamed node) never merge either.

        Args:
            a: One node id.
            b: The other node id.

        Returns:
            Whether the composer must show, and address, the two ids as one node.
        """
        if a == b:
            return True
        if not (a.startswith(b) or b.startswith(a)):
            return False
        named = self._resolve_entry(a)
        return named != a and named == self._resolve_entry(b)

    def _aliases(self, node: str) -> set[str]:
        """All the ids in the graph that are the same node as ``node``: its stubs.

        The other half of :meth:`_same_node`. A merged suggestion must also act as the
        merged node when it is the anchor. Thus the next step from it finds all the nodes
        that any of its ids was heard to talk to. Without this method, a merge of ``3d``
        into ``3d63c6429436`` would silently lose all that the composer observed only
        under the stub.

        The result is cached for each node, because this method goes through the full
        graph, and :meth:`_suggestions` runs several times for each key press (for the
        row list, the width of the dialog, and the paint). The topology does not change
        while the screen is open (a neighbour request builds a new topology). Thus the
        cached answer cannot become out of date.
        """
        ids = self._alias_cache.get(node)
        if ids is not None:
            return ids
        ids = {node}
        for link in self._topology.links():
            for end in (link.a, link.b):
                if end not in ids and self._same_node(end, node):
                    ids.add(end)
        self._alias_cache[node] = ids
        return ids

    def _pooled(self, one, other):  # noqa: ANN001, ANN201
        """Merge two suggestions for the same node into one row, with their evidence added.

        The id that stays is the longer of the two ids. It is the more specific identity,
        and it addresses the node best when the spec is rendered. The function adds the
        readings together exactly as the topology adds evidence when it merges a stub
        itself. Thus the ``n×`` and the median SNR of the row describe the full node, not
        the half that was ranked first by chance.
        """
        node = one.node if len(one.node) >= len(other.node) else other.node
        stamps = [link.last_seen for link in (one.link, other.link) if link.last_seen is not None]
        ends = sorted((self._anchor(), node))
        return HopSuggestion(
            node=node,
            link=Link(
                a=ends[0],
                b=ends[1],
                samples=one.link.samples + other.link.samples,
                snrs=[*one.link.snrs, *other.link.snrs],
                last_seen=max(stamps) if stamps else None,
                sources=one.link.sources | other.link.sources,
            ),
            strength=max(one.strength, other.strength),
        )

    def _suggestions(self) -> list:
        """The current suggestion rows, with duplicates merged and the typed entry as filter.

        Target mode removes the target (the path turns at it implicitly) and each hop that
        is already used. On the outbound leg, a second visit to a hop is never useful,
        because the mirror already goes over it again. Path mode removes only our node and
        the two neighbours of the insertion slot (each of them would make the inserted hop
        a loop to itself). A return leg can correctly use outbound repeaters again. Each
        exclusion also covers the aliases of the excluded node, so the composer cannot
        offer a stub of the anchor as a step from the anchor.

        The function collects the suggestions from all the aliases of the anchor, then
        merges them in pairs (:meth:`_same_node`, :meth:`_pooled`). Thus a node that the
        graph holds under a stub and under a full id appears one time, with all of its
        evidence, ranked by its best link.

        Returns:
            The :class:`~meshterm.services.topology.HopSuggestion` list for the anchor of
            the insertion slot (filtered, if there is a typed entry), with a maximum of
            :data:`_MAX_SUGGESTIONS` items.
        """
        if self._mirrored:
            exclude = frozenset({self._topology.self_id, self._target_id, *self._hops})
        else:
            after = self._hops[self._cursor] if self._cursor < len(self._hops) else None
            exclude = frozenset(
                {self._topology.self_id, self._anchor()} | ({after} if after else set())
            )
        merged: list = []
        for tail in sorted(self._aliases(self._anchor()), key=len, reverse=True):
            for suggestion in self._topology.next_hops(tail, exclude=exclude):
                if any(self._same_node(suggestion.node, other) for other in exclude):
                    continue
                at = next(
                    (i for i, m in enumerate(merged) if self._same_node(m.node, suggestion.node)),
                    None,
                )
                if at is None:
                    merged.append(suggestion)
                else:
                    merged[at] = self._pooled(merged[at], suggestion)
        merged.sort(key=lambda s: (-s.strength, s.node))
        needle = self._entry.strip().lower()
        if needle:
            merged = [
                s
                for s in merged
                if s.node.startswith(needle) or needle in self._resolve_entry(s.node).lower()
            ]
        return merged[:_MAX_SUGGESTIONS]

    def _custom_hex(self) -> str | None:
        """The typed entry as a hex hop that the user can add, or ``None`` if it is not one."""
        needle = self._entry.strip().lower().removeprefix("0x")
        return needle if is_path_hash(needle) else None

    def _rows(self) -> list[tuple[str, object]]:
        """The rows that the highlight can go to: custom hop, suggestions, fetch, actions."""
        rows: list[tuple[str, object]] = []
        custom = self._custom_hex()
        if custom:
            rows.append(("custom", custom))
        rows.extend(("hop", s) for s in self._suggestions())
        # When the slot is after a repeater for which we have credentials, its live
        # neighbour table is one key press away. The row goes with the suggestions,
        # because it adds to them: if you do not see the node that you need, ask the
        # repeater.
        if self._anchor() in self._fetch_nodes:
            rows.append(("fetch", self._anchor()))
        rows.append(("action", _USE))
        if self._mirrored:  # a path walk has no target for the device to route to
            rows.append(("action", _AUTO))
        return rows

    def _spec(self) -> str:
        """Render the composed route as the spec that the trace command will send.

        Target mode adds the target and the mirrored return leg at the end (refer to
        :func:`~meshterm.services.topology.render_forced_spec`). Path mode renders the
        composed walk exactly (:func:`~meshterm.services.topology.render_custom_spec`).
        The spec is empty until the walk has one hop or more.
        """
        if self._mirrored:
            return render_forced_spec(tuple(self._hops), self._target_hash, self._width_bytes)
        return render_custom_spec(tuple(self._hops), self._width_bytes)

    def _walk_nodes(self) -> list[str]:
        """The full walk that the current composition transmits, as canonical ids.

        In target mode, the walk goes out and comes back the same way: the outbound hops,
        the target, and the mirrored return. In path mode, it is the composed hops
        exactly. The check of record eligibility runs over this walk. The mirror halves
        are important, because a repeated section of the outbound leg also repeats on the
        return leg.
        """
        if self._mirrored:
            return [*self._hops, str(self._target_id), *reversed(self._hops)]
        return list(self._hops)

    # --- rendering ---------------------------------------------------------------

    def _path_entry(self, node: str) -> str | None:
        """A canonical id as a hop entry for :func:`path_text` (``None`` is our node).

        When the full key prefix of the pinned target is known, the entry is that prefix.
        Thus its hash annotation shows the spec width, also when that width is more than
        the 12 hex digits of the canonical id.
        """
        if node == self._topology.self_id:
            return None
        if node == self._target_id:
            return self._target_hash or node
        return node

    def _resolve_entry(self, entry: str) -> str:
        """Resolve an entry to its name: the label of the target, or else the name of a node.

        The method uses two resolvers, in order, because they answer different questions.
        The topology names a node that it identified: a hop hash that matches the prefix
        of exactly one contact is that contact, so its canonical id has the name. But a
        short hash (a 1-byte hop, which is the usual case at the default width of the
        protocol) often matches several contacts. The topology does not guess. It keeps
        the hash as it is, and knows no name for it. The resolver of the owning screen
        (:func:`~meshterm.services.trace_runner.make_node_resolver`) is the same
        first-match lookup that the trace screens use to name their walked hops. Thus,
        with this fallback, a node that the trace screen shows as "Lakeside" is
        "Lakeside" here too, not a bare ``3d`` that the user must decode. The method
        invents nothing: a hash with no match is returned unchanged, and renders as a
        hash.

        The results are cached, because the two resolvers scan the contact list, and this
        method now runs over each link in the graph before each paint (refer to
        :meth:`_aliases`). The topology and the resolver do not change while a composer is
        open (a neighbour request builds a new screen). Thus the answers stay the same
        for the life of the composer.
        """
        if self._target_id is not None and entry == (self._target_hash or self._target_id):
            return self._target_label or entry
        named = self._names.get(entry)
        if named is None:
            named = self._topology.display_name(entry) or self._resolve(entry) or entry
            self._names[entry] = named
        return named

    def _node_text(self, node: str, *, dim: bool = False) -> Text:
        """One route node, through the only path widget, in the trace format.

        When the name is known: ``Name (hash)``, with the name in its hue (the same in all
        the app), and our node in the white ``you`` style. When the name is not known: a
        bare hash in the ``brand`` style. Hashes show at the preferred path-hash width of
        the spec. All the other screens of the trace features truncate to the same width,
        so a node has the same length everywhere.

        Args:
            node: The canonical id of the node (or ``self_id`` for our node).
            dim: Whether to render in the uniform faint style of the resolved return leg,
                instead of the normal route colours.
        """
        return path_text(
            [self._path_entry(node)],
            self._resolve_entry,
            prefix_bytes=self._width_bytes,
            self_name=self._device_label,
            show_hash=True,
            hash_bytes=self._width_bytes,
            device_hash=self._device_hash or None,
            dim_from=0 if dim else None,
        )

    def _route_preview(self) -> PathLine:
        """The route that the user builds, with the endpoints filled in automatically.

        The only path widget, drawn exactly as the trace screen that opened this dialog
        draws its route lane. The composer is a live preview of that lane, so the two must
        be the same picture. Target mode shows the composed outbound leg in full colour.
        Immediately after it, it shows the pinned target and the mirrored return, resolved
        and dimmed (``dim_from``). The dimmed part means "you do not compose this half".
        Path mode shows each composed hop in full colour (the user composes all of them),
        between our node at the two ends. Only the last step back to our node is dimmed.

        Our two ends are bare (``bare_self``). A composed walk always starts at our node
        and comes back to our node, so the ``★`` says this in one cell. The remaining width
        of the dialog is for the hops that the user selects. A named hop shows no hash
        either. The hex that the spec transmits is not what the user chooses here. The hex
        would only fill the route that this dialog is for. That is also why the "Use this
        path" row does not spell the route again. A hop with no name still shows as its
        hash, truncated to the path-hash width that the session chose.

        The insertion slot is in the route, not between two of its hops. The ``+`` slot
        (:data:`~meshterm.ui.pathline.CURSOR_GLYPH`) is where the next selected node will
        be, in the ``cursor`` white of the highlight below it. The slot is a position, not
        a gap. Thus the preview stays the same picture that the trace screen draws, with
        its chips, and it does not have to use arrows only to get a seam for the slot.
        Also, a wrapped route keeps the slot like any other hop, and the slot is not left
        alone at a line break.
        """
        entries: list[str | None] = [None]
        entries.extend(self._path_entry(hop) for hop in self._hops)
        if self._mirrored:
            entries.append(self._path_entry(self._target_id))
            entries.extend(self._path_entry(hop) for hop in reversed(self._hops))
        entries.append(None)
        return path_line(
            entries,
            self._resolve_entry,
            prefix_bytes=self._width_bytes,
            self_name=self._device_label,
            hash_bytes=self._width_bytes,
            device_hash=self._device_hash or None,
            dim_from=(2 if self._mirrored else 1) + len(self._hops),
            bare_self=True,
            # The entries start with our ★, so arrow index k (the slot after shown node k)
            # is rendered-hop index k + 1.
            cursor=self._cursor + 1,
        )

    def _suggestion_text(self, suggestion) -> Text:  # noqa: ANN001
        """One suggestion row: the node, then the evidence of its link."""
        text = self._node_text(suggestion.node)
        link = suggestion.link
        snr = link.median_snr
        if snr is not None:
            text.append("  ↔ ", style="muted")
            text.append(f"{snr:+.1f} dB", style=snr_style(snr))
        text.append(f"  {link.samples}×", style="muted")
        age = format_age(age_seconds(link.last_seen))
        text.append(f" · {age}", style="muted")
        tags = "".join(_SOURCE_TAGS[s] for s in sorted(link.sources & _SOURCE_TAGS.keys()))
        if tags:
            text.append(f" · {tags}", style="faint")
        return text

    def _row_text(self, kind: str, payload: object) -> Text:
        """The text to show for one row that the highlight can go to."""
        if kind == "custom":
            text = Text("+ add hop ", style="warn")
            text.append(str(payload), style="brand")
            text.append("  (typed hex)", style="muted")
            return text
        if kind == "hop":
            return self._suggestion_text(payload)
        if kind == "fetch":
            text = Text("⇣ Fetch neighbours from ", style="")
            text.append_text(self._node_text(str(payload)))  # named like each other row
            text.append("  (asks the repeater over the mesh)", style="muted")
            return text
        if payload == _USE:
            label = Text.assemble(("✓ ", "ok"), "Use this path")
            # "This path" is the path that is pinned two rows above, in colour and at full
            # width. The dialog is there to shape it, and never lets it scroll away. This
            # row once spelled it again as raw hex. That said the same thing less well, and
            # the row became one hop longer each time that the route did (JP, 2026-08-10).
            # Only the empty case still must have words: a row that cannot act must say
            # why.
            if not self._spec():
                label.append("  (add a hop first)", style="muted")
            return label
        return Text("Auto — let the device route")

    @property
    def dialog_width(self) -> int:
        """The natural outer width, which fits the widest row (the compositor sets a limit)."""
        widths = [cell_len(self.title), cell_len(self.footer_hint)]
        # The preview has its own insertion slot, so the measured line is the drawn line:
        # the same hops, the same mode, and the same width.
        widths.append(self._route_preview().text().cell_len)
        if first_repeated_edge(self._walk_nodes()) is not None:
            widths.append(cell_len(_NOT_A_TRAIL))
        for kind, payload in self._rows():
            widths.append(cell_len(self._row_text(kind, payload).plain) + 2)
        return max(widths, default=20) + 8

    def render_body(self, width: int) -> list[str]:
        """Render the pinned route preview and filter line, then the rows of the list window.

        The preview and the heading do not move. The suggestion and action rows scroll in
        the space that remains in the dialog (refer to :class:`ListWindow`). Thus a long
        list of suggestions can never push the route that the user composes out of the
        dialog.
        """
        rows = self._rows()
        self._index = max(0, min(self._index, len(rows) - 1))

        # The preview breaks at hop boundaries, with a hanging indent of 2 cells (never in
        # a name, never in a chip). The insertion slot is one of those hops, so it goes on
        # a line like the other hops, and it is always visible.
        lines = [
            render_to_ansi(line, width, no_wrap=True)
            for line in self._route_preview().wrapped(width, indent=2)
        ]
        if first_repeated_edge(self._walk_nodes()) is not None:
            lines.extend(render_lines(Text(_NOT_A_TRAIL, style="warn"), width))
        lines.append("")
        if self._entry:
            lines.append(query_line(self._entry, width))
        else:
            heading = Text("Next hop from ", style="muted")
            heading.append_text(self._node_text(self._anchor()))
            heading.append(" — strongest first", style="muted")
            lines.extend(render_lines(heading, width))

        # Each entry of the list window is one rendered line, tagged with its row index
        # (``None`` is a spacer line or a note line, where the highlight cannot go).
        entries: list[tuple[int | None, str]] = []
        for i, (kind, payload) in enumerate(rows):
            if kind == "action" and (i == 0 or rows[i - 1][0] != "action"):
                entries.append((None, ""))  # a spacer separates the action group
            is_sel = i == self._index
            text = Text("❯ " if is_sel else "  ", style="cursor" if is_sel else "")
            text.append_text(self._row_text(kind, payload))
            if is_sel:
                text.style = "cursor"
            text.no_wrap = True
            # A suggestion row names a node in the same format as the route, so it is cut
            # like the route: a chip that goes past the edge of the dialog breaks on a half
            # block, and prose keeps the ellipsis.
            text = cut_to(text, width)
            text.no_wrap = True
            entries.append((i, render_to_ansi(text, width)))
        if not any(kind == "hop" for kind, _ in rows):
            if any(kind == "fetch" for kind, _ in rows):
                note = (
                    "(no observed links from here — fetch the repeater's neighbours, "
                    "or type a hex hash)"
                )
            else:
                note = "(no observed links from here — type a hex hash to force a hop)"
            # One entry for each wrapped line. A string of more than one line in one entry
            # would put a newline into the row arithmetic of the list window. It would also
            # get past the width contract, which measures each line (at 53 cells, the note
            # wraps).
            for note_line in render_lines(Text(note, style="muted"), width):
                entries.append((None, note_line))

        win = max(3, self._scroll_viewport - len(lines))
        at = next(p for p, (row, _line) in enumerate(entries) if row == self._index)
        top, count = self._list.fit(len(entries), win, at)
        self._cursor_row = None
        if top > 0:
            lines.append(render_to_ansi(ListWindow.marker(top, "above"), width))
        for pos in range(top, top + count):
            row, line = entries[pos]
            if row == self._index:
                self._cursor_row = len(lines)
            lines.append(line)
        below = len(entries) - top - count
        if below > 0:
            lines.append(render_to_ansi(ListWindow.marker(below, "below"), width))
        self._scroll_total = max(1, len(lines))
        return lines

    def cursor_line(self) -> int | None:
        """The body line of the highlighted row, so the session keeps it visible."""
        return getattr(self, "_cursor_row", None)

    # --- input ---------------------------------------------------------------------

    def _insert_hop(self, node: str) -> None:
        """Insert a hop at the insertion slot, and move the slot past the new hop."""
        self._hops.insert(self._cursor, node)
        self._cursor += 1
        self._entry = ""
        self._index = 0

    def _commit_row(self) -> None:
        """Apply the highlighted row: insert a hop at the insertion slot, or run an action."""
        rows = self._rows()
        if not rows:
            return
        kind, payload = rows[self._index]
        if kind == "custom":
            self._insert_hop(str(payload))
        elif kind == "hop":
            self._insert_hop(payload.node)  # type: ignore[union-attr]
        elif kind == "fetch":
            self.resolve(FetchNeighbours(node=str(payload)))
        elif payload == _USE:
            spec = self._spec()
            if spec:  # an empty path walk would look the same as AUTO_SPEC
                self.resolve(spec)
        elif payload == _AUTO:
            self.resolve(AUTO_SPEC)

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight or slot, edit the entry, add or remove hops, confirm, or cancel."""
        rows = self._rows()
        # The two ends clamp, and do not wrap: this list scrolls in a list window (refer to
        # ListWindow). A highlight that jumped from the end to the start would move the full
        # list window with it. That is the only move that looks as if the screen changed
        # below the user.
        if action == "up" and rows:
            self._index = max(0, self._index - 1)
        elif action == "down" and rows:
            self._index = min(len(rows) - 1, self._index + 1)
        elif action == "pageup" and rows:
            self._index = max(0, self._index - self._list.page)
        elif action == "pagedown" and rows:
            self._index = min(len(rows) - 1, self._index + self._list.page)
        elif action in ("home", "ctrl_home"):
            self._index = 0
        elif action in ("end", "ctrl_end"):
            self._index = max(0, len(rows) - 1)
        elif action == "left" and self._cursor:
            self._cursor -= 1  # the anchor moved, so the suggestions below change
            self._index = 0
        elif action == "right" and self._cursor < len(self._hops):
            self._cursor += 1
            self._index = 0
        elif action == "enter":
            self._commit_row()
        elif action == "backspace":
            if self._entry:
                self._entry = self._entry[:-1]
            elif self._cursor:
                self._hops.pop(self._cursor - 1)
                self._cursor -= 1
            self._index = 0
        elif action == "text" and data.isprintable():
            if not data.isspace() or self._entry:  # never start the entry with a space
                self._entry += data
                self._index = 0
        elif action == "escape":
            # The typed entry is the most recent thing that the user entered, so Esc clears
            # it first. ⌫ already has the same order here (the entry, then a hop), and this
            # is the rule for each find-as-you-type screen in the app.
            if self._entry:
                self._entry = ""
                self._index = 0
            else:
                super().handle("escape")
