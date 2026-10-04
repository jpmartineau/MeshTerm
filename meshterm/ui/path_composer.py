# SPDX-License-Identifier: Apache-2.0
"""The trace path composer: build a forced route hop by hop, guided by observed links.

A floating dialog over a live trace screen. The route under construction reads across
the top — ``★ → hop → … → ★`` — and beneath it sits a suggestion list: the nodes the
topology evidence says the path's current tail can hear, strongest observed link first
(see :meth:`~meshterm.services.topology.MeshTopology.next_hops`). Every link is
bidirectional evidence, so a path that was ever *received* through two nodes proposes
that link in either direction.

The composer serves both trace features, and which one owns it decides how the route's
return half is written (the trace protocol has no separate return-path field — the spec
sent to the device is one walk that ends within our earshot):

* **Target mode** (*Trace target* — a ``target_id`` is pinned): only the outbound leg
  is composed. The target stays pinned as the turning point and the return is the
  outbound hops mirrored — the preview resolves and dims that half, since it isn't
  yours to choose. An *Auto* action hands routing back to the device.
* **Path mode** (*Trace path* — no target at all): the whole walk is yours, hop by
  hop, until the route comes back within earshot of us — only the final landing on
  our own node is filled in (and dimmed). The preview opens as just ``★ → + → ★``.
  There is no *Auto* action, because with no destination there is nothing for the
  device to route to.

Interaction, following the reorder screen's cursor-over-rows-and-actions pattern:

* ↑/↓ move over suggestions and the action rows; Enter on a suggestion inserts it
  at the route's insertion cursor.
* ←/→ slide that insertion cursor along the editable leg — it rides *in* the route as
  a hop of its own, the ``cursor``-white ``+`` slot the next chosen node drops into — so a
  hop can be spliced in (at the slot) or removed (⌫, to its left) anywhere in the
  route, not just at the end. It opens at the end of the composed leg, where inserting
  is appending — the classic flow unchanged. The suggestion list follows: it always
  proposes next hops from the node left of the cursor.
* Typing filters the suggestions by name or hash — and when the typed text is itself
  even-length hex, an *add custom hop* row appears, so a node we have never observed
  (or a bare hash from another tool) can be forced into the route.
* Backspace erases the filter first; with the filter empty it removes the hop left
  of the cursor.
* When the cursor stands after a repeater we hold admin credentials for, a *fetch
  neighbours* row asks that repeater over the mesh for its own neighbour table —
  fresh second-vantage evidence exactly where composing ran out of it. The dialog
  resolves :class:`FetchNeighbours` and the owning flow fetches, refreshes the
  topology, and reopens the composer mid-thought (hops and cursor preserved).
* Enter on **Use this path** commits the composed spec; Esc cancels with no change.
* A walk that crosses some link twice in the same direction shows a yellow ⚠ note
  under the preview: still walkable, but no longer a *trail*, so the trophy case
  will ignore it (the no-cheat rule — see :mod:`~meshterm.services.records`).

Nodes render through the shared path widget in the trace flavour — ``Name (hash)``
with names in their app-wide hues, the hash at the session's chosen path-hash width,
and a hop the topology can't name falling back to the owning screen's own resolver
(see :meth:`PathComposerScreen._resolve_entry`), so a node reads the same here as in
the trace window this dialog floats over. The route preview is that same trace-window
route lane, live: :mod:`~meshterm.ui.pathline` chips (or arrows), wrapped at hop
boundaries, our two ends the bare ``★``, and no hash repeated after a name. That preview
*is* the path, so the *Use this path* row doesn't respell it: the row names the action and
the picture above it names the route. The suggestion list scrolls in a window under the pinned route
preview (faint ``↑/↓ n more`` markers at its edges; PgUp/PgDn stride by a windowful),
so the route under construction never leaves the screen.
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

#: Sentinel spec meaning "no forced path — let the device route" (the trace screen's
#: empty-spec convention). Only meaningful in target mode; a path walk has no target
#: for the device to route to, so path mode never resolves it.
AUTO_SPEC = ""

#: How many suggestions to show at most; beyond this the evidence is too weak to matter
#: and the dialog would outgrow the screen.
_MAX_SUGGESTIONS = 12

#: Action-row sentinels (kept distinct from suggestion rows, which carry node ids).
#: There is no cancel sentinel: leaving is Esc's, and a row that only pressed Esc for
#: you cost a cursor stop above the row that commits.
_USE = "use"
_AUTO = "auto"

#: One-letter tags for the evidence classes backing a link, shown beside each
#: suggestion: T(race), R(oute — the firmware's learned out_path), P(acket log),
#: N(eighbour table fetched from a repeater).
_SOURCE_TAGS = {"trace": "T", "route": "R", "packet": "P", "neighbour": "N"}

#: The inline no-cheat warning, shown under the route preview while the composed walk
#: crosses some link twice in the same direction — repeating a stretch would let its
#: score be farmed, so the trophy case disqualifies such a walk (it is no longer a
#: *trail*, graph theory's walk-with-no-repeated-edge; see
#: :func:`~meshterm.services.records.first_repeated_edge`). Composing one stays legal —
#: the warning only says the walk can't set records.
_NOT_A_TRAIL = "⚠ repeats a link — not a trail, so records ignore this walk"


@dataclass(frozen=True, slots=True)
class FetchNeighbours:
    """The composer's resolution when the user asks a repeater for its neighbours.

    The dialog itself never touches the radio; it resolves this marker and the owning
    flow performs the login + fetch, refreshes the topology, and reopens the composer
    with :attr:`PathComposerScreen.hops` and :attr:`PathComposerScreen.cursor`
    re-seeded.

    Attributes:
        node: Canonical id of the repeater to query (the insertion cursor's anchor
            when committed).
    """

    node: str


class PathComposerScreen(Screen):
    """Compose a forced trace path step by step, mirrored at a target or hand-routed.

    Resolves with the finished spec (comma-separated hex — in target mode the outbound
    hops, the target, then the mirrored return; in path mode the composed walk
    verbatim), :data:`AUTO_SPEC` for device routing (target mode only), or
    :data:`~meshterm.ui.tui.screen.CANCEL`.

    The dialog only ever grows (see :attr:`~meshterm.ui.tui.screen.Screen.grow_only`):
    every added hop widens the route preview and each new tail swaps in a different
    suggestion set, so a size-to-content box would twitch on every keystroke. Instead
    the box holds its tallest and widest extent for the composing session, like the
    packet viewer.
    """

    grow_only = True

    #: The composing keys, in the hint grammar's navigation-then-actions-then-Esc order.
    #: Esc's verb is the only part that moves — see :attr:`footer_hint`.
    #: ``←→ slot``, not ``←→ cursor``: this app's *cursor* is the row the ❯ points at,
    #: which is what ↑↓ moves here — the arrows move the **insertion slot** along the path
    #: being built, the app's own word for it (and the one thing besides the row cursor
    #: that wears the selection white).
    _HINT = "↑↓ move · ←→ slot · Enter add · type to filter · ⌫ remove · Esc cancel"

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The composing keys, with ``Esc clear`` standing in while an entry is typed.

        Esc peels the typed entry before it abandons the composer, exactly as ⌫ peels it
        before it removes a hop, so the line says which of the two the press will do (the
        app-wide filter rule — the map, the mesh walk and every select list read the same).
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

        A pinned target (all three ``target_*`` arguments) selects target mode: the
        target is excluded from suggestions (the path implicitly turns at it) and the
        emitted spec appends it plus the mirrored return. With no target the composer
        is in path mode — the whole walk is composed by hand and committed verbatim.

        Args:
            device_label: Our own node's name, opening and closing the route preview.
            topology: The evidence graph suggestions are drawn from.
            width_bytes: Preferred per-hop path-hash width (bytes) for the emitted spec.
            target_id: The pinned target's canonical id, or ``None`` for path mode.
            target_hash: The target's hex hash used as the symmetric spec's turning
                point (its full key prefix; truncated to the spec width at commit).
            target_label: The target's display name for the route preview.
            device_hash: Our own public key, so the preview's endpoints carry a hash
                exactly like the trace screen's route line.
            hops: Canonical ids of already-composed hops (reopening the dialog resumes
                where the user left off).
            cursor: Arrow index the insertion cursor resumes at (reopening after a
                fetch); ``None`` parks it at the path's end, the append position.
            fetch_nodes: Canonical ids whose live neighbour table can be fetched
                (repeater contacts with a public key); when the path's tail is one of
                them, the *fetch neighbours* row appears.
            resolve: The owning screen's node resolver — the fallback that names a hop
                the topology can't (see :meth:`_resolve_entry`), so the composer reads
                the same names the screen that opened it does.
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
        #: Resolved display names by node id — see :meth:`_resolve_entry`.
        self._names: dict[str, str] = {}
        #: The graph ids each node reads as — see :meth:`_aliases`.
        self._alias_cache: dict[str, set[str]] = {}
        self._width_bytes = width_bytes
        self._hops: list[str] = list(hops or [])
        #: The insertion cursor: which joining arrow of the editable leg it sits on.
        #: Arrow k stands between display node k (us at 0, else hop k) and its
        #: successor — an insert lands at hops index k, ⌫ removes hop k-1.
        self._cursor = len(self._hops) if cursor is None else max(0, min(cursor, len(self._hops)))
        self._fetch_nodes = fetch_nodes
        self._entry = ""
        self._index = 0
        #: The suggestion/action list's window under the pinned route preview.
        self._list = ListWindow()

    @property
    def hops(self) -> list[str]:
        """The composed hops so far (for re-seeding after a fetch)."""
        return list(self._hops)

    @property
    def cursor(self) -> int:
        """The insertion cursor's arrow index (for re-seeding after a fetch)."""
        return self._cursor

    # --- state -----------------------------------------------------------------

    @property
    def _mirrored(self) -> bool:
        """Whether the return leg is the pinned target's mirror (target mode)."""
        return self._target_id is not None

    def _anchor(self) -> str:
        """The node just left of the insertion cursor (us while the cursor is home).

        Suggestions, the fetch row, and the *Next hop from* heading all follow this
        anchor — an insert continues the walk from here, wherever the cursor stands.
        """
        return self._hops[self._cursor - 1] if self._cursor else self._topology.self_id

    def _same_node(self, a: str, b: str) -> bool:
        """Whether two graph ids read as one node — same name, one prefixing the other.

        The evidence graph keeps a hop hash it can't pin down as its own vertex: a
        1-byte ``3d`` that prefix-matches two contacts stays ``3d``, separate from the
        ``3d63c6429436`` its wider sightings landed on. Both are real ids with real
        evidence, and the graph is right not to guess — but on screen, once the name
        resolver has named them (see :meth:`_resolve_entry`), they are one row shown
        twice. The test is deliberately both halves: a shared *name*, and one hash a
        prefix of the other. Two unnamed hashes never merge (they resolve to
        themselves, so the names differ) and two same-named nodes on unrelated keys —
        a duplicate contact name, a renamed node — never merge either.

        Args:
            a: One node id.
            b: The other.

        Returns:
            Whether the two should be shown, and addressed, as a single node.
        """
        if a == b:
            return True
        if not (a.startswith(b) or b.startswith(a)):
            return False
        named = self._resolve_entry(a)
        return named != a and named == self._resolve_entry(b)

    def _aliases(self, node: str) -> set[str]:
        """Every id in the graph that reads as ``node`` — the stubs it is split across.

        The other half of :meth:`_same_node`: a merged suggestion must also *stand* as
        the merged node, so the next step out of it sees everything all its ids have
        been heard talking to. Without this, folding ``3d`` into ``3d63c6429436`` would
        quietly cost the composer whatever was only ever observed under the stub.

        Cached per node, since this walks the whole graph and :meth:`_suggestions` runs
        several times per keystroke (the row list, the dialog's width, the paint). The
        topology is fixed for the screen's lifetime — a neighbour fetch builds a fresh
        one — so the answer can't go stale under the cache.
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
        """Fold two suggestions for the same node into one row, evidence pooled.

        The surviving id is the longer of the two — the more specific identity, and the
        one that addresses the node best when the spec is rendered. The readings behind
        it are summed exactly as the topology pools evidence when it coalesces a stub
        itself, so the row's ``n×`` and median SNR describe the whole node rather than
        whichever half happened to rank first.
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
        """The current suggestion rows — duplicates merged, filtered by the typed entry.

        Target mode excludes the target (the path implicitly turns at it) and every
        used hop (revisiting one on the outbound leg is never useful — the mirror
        already recrosses it). Path mode only excludes ourselves and the cursor's
        two neighbours (either would make the inserted hop a self-loop): a return
        leg legitimately reuses outbound repeaters. Every exclusion covers the excluded
        node's aliases too, so a stub of the anchor can't be proposed as a step off it.

        Suggestions are gathered from all of the anchor's aliases and then folded
        pairwise (:meth:`_same_node`, :meth:`_pooled`), so a node the graph holds under
        both a stub and a full id appears once, with all of its evidence, ranked by its
        best link.

        Returns:
            The (possibly filtered) :class:`~meshterm.services.topology.HopSuggestion`
            list for the insertion cursor's anchor, capped at :data:`_MAX_SUGGESTIONS`.
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
        """The typed entry as an addable hex hop, or ``None`` when it isn't one."""
        needle = self._entry.strip().lower().removeprefix("0x")
        return needle if is_path_hash(needle) else None

    def _rows(self) -> list[tuple[str, object]]:
        """The cursor-addressable rows: custom hop, suggestions, fetch, then actions."""
        rows: list[tuple[str, object]] = []
        custom = self._custom_hex()
        if custom:
            rows.append(("custom", custom))
        rows.extend(("hop", s) for s in self._suggestions())
        # The cursor standing after a repeater we hold credentials for, its live
        # neighbour table is one keypress away — placed with the suggestions, because
        # that is what it extends: "don't see the node you need? ask the repeater."
        if self._anchor() in self._fetch_nodes:
            rows.append(("fetch", self._anchor()))
        rows.append(("action", _USE))
        if self._mirrored:  # a path walk has no target for the device to route to
            rows.append(("action", _AUTO))
        return rows

    def _spec(self) -> str:
        """Render the composed route as the spec the trace command will send.

        Target mode appends the target and the mirrored return leg (see
        :func:`~meshterm.services.topology.render_forced_spec`); path mode renders
        the composed walk verbatim (:func:`~meshterm.services.topology.
        render_custom_spec`) — empty until it has at least one hop.
        """
        if self._mirrored:
            return render_forced_spec(tuple(self._hops), self._target_hash, self._width_bytes)
        return render_custom_spec(tuple(self._hops), self._width_bytes)

    def _walk_nodes(self) -> list[str]:
        """The whole walk the current composition transmits, as canonical ids.

        Target mode is the symmetric boomerang (outbound hops, the target, the
        mirrored return); path mode is the composed hops verbatim. This is what the
        record-eligibility check runs over — the mirror halves matter, because a
        repeated outbound stretch repeats on the return leg too.
        """
        if self._mirrored:
            return [*self._hops, str(self._target_id), *reversed(self._hops)]
        return list(self._hops)

    # --- rendering ---------------------------------------------------------------

    def _path_entry(self, node: str) -> str | None:
        """A canonical id as a :func:`path_text` hop entry (``None`` = our device).

        The pinned target rides as its full hash when one is known, so its hash
        annotation shows the spec width even past the canonical id's 12 hex.
        """
        if node == self._topology.self_id:
            return None
        if node == self._target_id:
            return self._target_hash or node
        return node

    def _resolve_entry(self, entry: str) -> str:
        """Resolve an entry to its display name — the target's label, else a node's.

        Two resolvers, in order, because they answer different questions. The topology
        names a node it has *identified*: a hop hash that prefix-matches exactly one
        contact is that contact, so its canonical id carries the name. But a short hash
        — a 1-byte hop, the common case at the protocol's default width — often matches
        several contacts, and rather than guess, the topology keeps it as itself and
        knows no name for it. The owning screen's resolver
        (:func:`~meshterm.services.trace_runner.make_node_resolver`) is the same
        first-match lookup the trace screens name their walked hops by, so falling back
        to it means a node the trace window shows as *Lakeside* is *Lakeside* here too,
        instead of a bare ``3d`` the reader has to decode. Nothing is invented: an
        unmatched hash comes back unchanged and renders as a hash.

        Memoized: both resolvers scan the contact list, and this now runs over every
        link in the graph on the way to each repaint (see :meth:`_aliases`). Neither
        the topology nor the resolver changes while a composer is open — a neighbour
        fetch builds a fresh screen — so the answers are stable for its lifetime.
        """
        if self._target_id is not None and entry == (self._target_hash or self._target_id):
            return self._target_label or entry
        named = self._names.get(entry)
        if named is None:
            named = self._topology.display_name(entry) or self._resolve(entry) or entry
            self._names[entry] = named
        return named

    def _node_text(self, node: str, *, dim: bool = False) -> Text:
        """One route node through THE path widget, the trace presentation.

        ``Name (hash)`` when known — the name in its app-wide hue, our own device
        the white ``you`` — a bare brand hash when not. Hashes show at the spec's
        preferred path-hash width, the same width every other trace-feature view
        truncates to, so a node reads the same length everywhere.

        Args:
            node: The node's canonical id (or ``self_id`` for our own device).
            dim: Whether to render in the resolved-return-leg's uniform faint style
                rather than the normal route colours.
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
        """The route under construction, endpoints filled in automatically.

        THE path widget, drawn exactly as the trace screen that opened this dialog
        draws its route lane — the composer is a live preview of that lane, so the two
        must be the same picture. Target mode shows the composed outbound in full
        colour, then the pinned target and the mirrored return resolved and dimmed
        right alongside it (``dim_from``) — the dimming reads as "this half isn't yours
        to compose". Path mode shows every composed hop in full colour (they are all
        yours) between our own node at both ends, with only the final landing back on
        us dimmed.

        Both our ends go bare (``bare_self``): a composed walk always leaves us and
        comes home to us, so the ``★`` says it in one cell and leaves the rest of the
        dialog's width to the hops being chosen. Named hops show no hash either: the hex
        the spec goes on the air as is not what anyone is choosing here — it would only
        crowd the route this dialog exists to shape, which is why the *Use this path* row
        no longer respells it either. An unnamed hop still reads as its hash, truncated
        to the session's chosen path-hash width.

        The insertion cursor is *in* the route, not between two of its hops: the
        ``+`` slot (:data:`~meshterm.ui.pathline.CURSOR_GLYPH`) sits where the next
        chosen node will, in the ``cursor`` white the row cursor below it wears. Drawing an
        insertion point as a position rather than as a gap is what lets the preview stay
        the picture the trace window paints — chips and all, rather than falling back to
        arrows for the sake of a seam to sit in — and lets a wrapped route carry the
        cursor like any other hop instead of stranding it on a line break.
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
            # The entries open on our own ★, so arrow index k — the slot after display
            # node k — is rendered-hop index k + 1.
            cursor=self._cursor + 1,
        )

    def _suggestion_text(self, suggestion) -> Text:  # noqa: ANN001
        """One suggestion row: node, then its link's evidence trail."""
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
        """The display text for one cursor-addressable row."""
        if kind == "custom":
            text = Text("+ add hop ", style="warn")
            text.append(str(payload), style="brand")
            text.append("  (typed hex)", style="muted")
            return text
        if kind == "hop":
            return self._suggestion_text(payload)
        if kind == "fetch":
            text = Text("⇣ Fetch neighbours from ", style="")
            text.append_text(self._node_text(str(payload)))  # named like every other row
            text.append("  (asks the repeater over the mesh)", style="muted")
            return text
        if payload == _USE:
            label = Text.assemble(("✓ ", "ok"), "Use this path")
            # "This path" is the one pinned two rows up, in colour and at full width — the
            # dialog exists to shape it and never lets it scroll away. Respelling it here
            # as raw hex (JP, 2026-08-10) said the same thing worse, and grew the row by a
            # hop every time the route did. Only the empty case still needs words: an
            # unarmed row has to say why.
            if not self._spec():
                label.append("  (add a hop first)", style="muted")
            return label
        return Text("Auto — let the device route")

    @property
    def dialog_width(self) -> int:
        """Natural outer width hugging the widest row (compositor still caps it)."""
        widths = [cell_len(self.title), cell_len(self.footer_hint)]
        # The preview carries its own cursor slot, so the measured line is the drawn
        # line — same hops, same mode, same width.
        widths.append(self._route_preview().text().cell_len)
        if first_repeated_edge(self._walk_nodes()) is not None:
            widths.append(cell_len(_NOT_A_TRAIL))
        for kind, payload in self._rows():
            widths.append(cell_len(self._row_text(kind, payload).plain) + 2)
        return max(widths, default=20) + 8

    def render_body(self, width: int) -> list[str]:
        """Render the pinned route preview and filter line, then the windowed rows.

        The preview and heading hold still; the suggestion/action rows scroll in
        whatever the dialog's budget has left (see :class:`ListWindow`), so a long
        suggestion list can never push the route being composed out of the dialog.
        """
        rows = self._rows()
        self._index = max(0, min(self._index, len(rows) - 1))

        # The preview breaks at hop boundaries under a 2-column hanging indent (never
        # mid-name, never mid-chip); the insertion slot is one of those hops, so it
        # lands on a line like the rest and is always visible.
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

        # Each windowable entry is one rendered line tagged with its row index
        # (``None`` = a spacer or note line the cursor can't land on).
        entries: list[tuple[int | None, str]] = []
        for i, (kind, payload) in enumerate(rows):
            if kind == "action" and (i == 0 or rows[i - 1][0] != "action"):
                entries.append((None, ""))  # a spacer sets the action group apart
            is_sel = i == self._index
            text = Text("❯ " if is_sel else "  ", style="cursor" if is_sel else "")
            text.append_text(self._row_text(kind, payload))
            if is_sel:
                text.style = "cursor"
            text.no_wrap = True
            # A suggestion row names a node in the route's own vocabulary, so it is cut
            # like one: a chip that runs off the dialog cracks, prose keeps the ellipsis.
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
            # One entry per wrapped line: a multi-line string in a single entry would
            # smuggle a newline into the windowed list's row math (and past the width
            # contract, which measures per line — at 53 columns the note wraps).
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
        """Splice a hop in at the insertion cursor, which advances past it."""
        self._hops.insert(self._cursor, node)
        self._cursor += 1
        self._entry = ""
        self._index = 0

    def _commit_row(self) -> None:
        """Apply the highlighted row: insert a hop at the cursor or run an action."""
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
            if spec:  # an empty path walk would masquerade as AUTO_SPEC
                self.resolve(spec)
        elif payload == _AUTO:
            self.resolve(AUTO_SPEC)

    def handle(self, action: str, data: str = "") -> None:
        """Move the cursors, edit the entry, add/remove hops, or commit/cancel."""
        rows = self._rows()
        # Both ends clamp rather than wrap: this list scrolls inside a window (see
        # ListWindow), and a highlight that jumped from the tail to the head would take the
        # whole window with it — the one move that looks like the screen changed under you.
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
            self._cursor -= 1  # the anchor moved: suggestions re-seat below
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
            if not data.isspace() or self._entry:  # never begin the entry with a space
                self._entry += data
                self._index = 0
        elif action == "escape":
            # The typed entry is the most recent thing the reader entered, so Esc peels it
            # first — the same layering ⌫ already has here (entry, then a hop) and the
            # app-wide rule for a find-as-you-type screen.
            if self._entry:
                self._entry = ""
                self._index = 0
            else:
                super().handle("escape")
