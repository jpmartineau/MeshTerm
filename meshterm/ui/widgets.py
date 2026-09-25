# SPDX-License-Identifier: Apache-2.0
"""Reusable Rich widgets: banner, status pill, trace tables, and progress bars."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING

from rich import box
from rich.cells import cell_len
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from .. import __version__
from ..core.channels import is_name_derived, is_public_channel, is_public_name
from ..core.models import (
    LOCAL_DEVICE_LABEL,
    NODE_TYPE_CHAT,
    NODE_TYPE_LABELS,
    NODE_TYPE_REPEATER,
    NODE_TYPE_ROOM,
    NODE_TYPE_SENSOR,
    Contact,
    HopAggregate,
    NameKeyResolver,
    NodeResolver,
    TraceResult,
    TraceStats,
    TxOptResult,
    utcnow,
)
from ..platforms import Platform, on_platform
from .marks import (
    DST_NODE,
    NODE_MARK,
    REPEATER_MARK,
    RGB,
    SELF_MARK,
    SRC_NODE,
    UNKNOWN_MARK,
    GlyphOf,
    LabelOf,
    LabelRgbOf,
    parse_hex,
)
from .pathline import SELF_GLYPH, PathHop, PathLine, path_line
from .theme import glyph, mark_rgb, name_style, node_style, snr_style

if TYPE_CHECKING:
    from ..core.discovery import DiscoveredDevice
    from ..core.regions import Scope


def channel_glyph(name: str, secret: bytes | None) -> str:
    """The one-character openness marker for a channel, shared across channel-facing screens.

    ``＃`` marks a name-derived (``#``-style) channel, ``🌐`` a fixed-key well-known public
    channel (e.g. the firmware default ``Public``), and ``🔒`` a private one — mapped
    through :func:`~meshterm.ui.theme.glyph`, so PicoCalc draws the compact ``# @ ⚿``
    instead. Within a platform every glyph is one width (double on regular, single on
    PicoCalc), so callers can prefix rows with ``"{glyph} "`` without disturbing column
    alignment. When the secret is unknown, the name alone is used to guess.

    Args:
        name: The channel name.
        secret: The channel's 16-byte secret, or ``None`` when only the name is known.

    Returns:
        A single-character glyph.
    """
    if secret is None:
        return glyph("＃") if is_public_name(name) else glyph("🔒")
    if is_name_derived(name, secret):
        return glyph("＃")
    if is_public_channel(name, secret):
        return glyph("🌐")
    return glyph("🔒")


#: The concept icon for a region — a flood's scope (see ``CLAUDE.md``'s icon table).
REGION_ICON = "🔖"


def scope_text(scope: Scope | None, *, bare: bool = False) -> Text:
    """THE rendering of a flood's scope, for every surface that states one.

    Three readings, one per :attr:`~meshterm.core.regions.Scope.state`:

    * ``scope yul`` — a scoped flood whose region is known, the name in the ``scope``
      style (a region is not a node, so it never takes a node hue);
    * ``unknown scope 3fa1`` (``? 3fa1`` bare) — a scoped flood no known name
      reproduces: the code is shown so
      two frames can still be told to share a region, ``muted`` because it names nothing;
    * ``unscoped`` — a plain flood, ``muted``: the ordinary case, stated not stressed.

    ``None`` (a direct frame, or one whose route type was never kept) draws nothing — a
    repeater never region-filters those, so a word there would claim a meaning it lacks.

    Args:
        scope: The frame's scope (:meth:`~meshterm.core.region_store.RegionStore.scope_of`).
        bare: Drop the ``scope`` lead-in and draw the region name alone, for a lane whose
            heading already says what it holds.

    Returns:
        The styled text (empty for ``None``).
    """
    text = Text()
    if scope is None:
        return text
    if scope.state == "scoped" and scope.region:
        if not bare:
            text.append("scope ", style="muted")
        text.append(scope.region, style="scope")
    elif scope.scoped:
        # No `·` inside the reading: a title chains its status atoms with `·`, and
        # ``scoped · 3fa1`` there read as two atoms where it is one fact.
        text.append("?" if bare else "unknown scope", style="muted")
        if scope.code:
            text.append(f" {scope.code}", style="muted")
    else:
        text.append("unscoped", style="muted")
    return text


def identity_label(label: str | None) -> str | None:
    """Default node resolver: leave labels untouched."""
    return label


def banner(
    profile: str | None, mock: bool, selected_device: DiscoveredDevice | None = None
) -> Panel:
    """Build the application header panel.

    Args:
        profile: Active device profile name, if any.
        mock: Whether the simulator is in use.
        selected_device: The discovered device chosen for this session, if any, shown
            when no named profile is in use.

    Returns:
        A Rich :class:`Panel` showing the app name, version, and connection target.
    """
    if mock:
        target = "[warn]simulator[/warn]"
    elif profile:
        target = profile
    elif selected_device is not None:
        target = selected_device.label
    else:
        target = "[muted]no device[/muted]"
    body = Text.from_markup(
        f"[brand]MeshTerm[/brand] [muted]v{__version__}[/muted]\n[muted]device:[/muted] {target}"
    )
    return Panel(body, border_style="accent", expand=False, title="[accent]Mesh[/accent]")


def make_progress(console: Console) -> Progress:
    """Create a themed progress bar for trace sweeps.

    Args:
        console: The console to render into.

    Returns:
        A configured :class:`rich.progress.Progress` (use as a context manager).
    """
    return Progress(
        SpinnerColumn(style="accent"),
        TextColumn("[accent]{task.description}"),
        BarColumn(complete_style="brand", finished_style="ok"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


def link_text(
    origin: str | None,
    destination: str | None,
    device_label: str,
    resolve: NodeResolver = identity_label,
    hash_bytes: int | None = None,
    device_hash: str | None = None,
) -> Text:
    """Render an ``origin -> destination`` link through THE path widget.

    A two-node :func:`path_text` in the trace presentation: a known node as
    ``name (hash)``, an unknown one as its bare hash, our own device as its label
    (plus ``device_hash`` when supplied). Hashes are truncated to ``hash_bytes`` so
    they match how the trace command addressed each node.

    Args:
        origin: The transmitting node (``None`` = our device).
        destination: The receiving node (``None`` = our device).
        device_label: The label used for our own device.
        resolve: Maps a raw hop hash to a friendly name when the node is known.
        hash_bytes: Path-hash width (bytes) to truncate shown hashes to.
        device_hash: Our own device's key/hash, shown alongside its label when known.

    Returns:
        A :class:`Text` like ``us (a1) → Alice (3d)`` with the arrow muted.
    """
    ends = [None if not node or node == device_label else node for node in (origin, destination)]
    return path_text(
        ends,
        resolve,
        prefix_bytes=hash_bytes or 8,
        self_name=device_label,
        show_hash=True,
        hash_bytes=hash_bytes,
        device_hash=device_hash,
    )


def traces_table(
    traces: list[TraceResult],
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = identity_label,
    device_hash: str | None = None,
) -> Table:
    """Render every trace's per-hop SNR side by side, one column per trace.

    Hops are framed as ``origin -> destination`` links (the first originates at our
    device, the last returns to it) and aligned by position across traces, so each
    column is one trace's SNR readings down the shared path. Each node is annotated with
    its hash at the command's path-hash width. A trailing ``min`` row shows each trace's
    bottleneck — the per-trace values the run's *median min SNR* summarizes. Traces that
    never replied appear as a ``✗`` column.

    Args:
        traces: The individual traces to display, in run order.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        device_hash: Our own device's key/hash, annotated onto the path's endpoints.

    Returns:
        A Rich :class:`Table` with a column per trace.
    """
    target = traces[0].target if traces else ""
    # All traces in a run share the command's path-hash width; take it from the first
    # successful one so node hashes render at the width they were addressed.
    hash_bytes = next((t.path_hash_bytes for t in traces if t.success), None)
    table = Table(title=f"Trace → {target}", border_style="muted", expand=False)
    table.add_column("HOP", justify="right", style="muted")
    table.add_column("FROM → TO")
    for i in range(1, len(traces) + 1):
        table.add_column(f"#{i}", justify="right")

    # Index each trace's edges by hop position so columns line up even when traces
    # take different-length paths (a missing hop shows as a muted dash).
    edges_by_trace = [
        {e.index: e for e in t.edges(device_label)} if t.success else {} for t in traces
    ]
    indices = sorted({idx for edges in edges_by_trace for idx in edges})
    for idx in indices:
        link = next(
            (
                link_text(
                    edges[idx].origin,
                    edges[idx].destination,
                    device_label,
                    resolve,
                    hash_bytes,
                    device_hash,
                )
                for edges in edges_by_trace
                if idx in edges
            ),
            Text("—", style="muted"),
        )
        row: list[Text] = [Text(str(idx)), link]
        for edges in edges_by_trace:
            edge = edges.get(idx)
            if edge is None:
                row.append(Text("—", style="muted"))
            else:
                row.append(Text(f"{edge.snr:+.1f}", style=snr_style(edge.snr)))
        table.add_row(*row)

    # Bottleneck per trace: the weakest link the run's median min SNR is taken over.
    min_row: list[Text] = [Text("min", style="muted"), Text("bottleneck", style="muted")]
    for trace in traces:
        if not trace.success:
            min_row.append(Text("✗", style="err"))
        elif trace.min_snr is not None:
            min_row.append(Text(f"{trace.min_snr:+.1f}", style=snr_style(trace.min_snr)))
        else:
            min_row.append(Text("—", style="muted"))
    table.add_section()
    table.add_row(*min_row)
    return table


def path_text(
    hops: Sequence[str | None],
    resolve: NodeResolver = identity_label,
    *,
    prefix_bytes: int = 0,
    self_name: str | None = None,
    empty: str = "direct",
    show_hash: bool = False,
    hash_bytes: int | None = None,
    device_hash: str | None = None,
    dim_from: int | None = None,
    hash_as_name: bool = False,
) -> Text:
    """Render a hop sequence compactly on one line — THE path widget.

    The one way MeshTerm shows a walked, relayed, or planned hop sequence, wherever one
    appears: a packet's ``via`` row, a message's delivery paths, a feed note, a trace's
    walked route or planned spec. Each hop renders as its resolved name — coloured in
    the app-wide per-name hue, our own node pure white — or, unnamed, as its hash in
    the ``node.unknown`` grey (colour marks an identified node, exactly as in the path
    graph; an unresolvable hop never wears a hue its bytes would derive). Hops are
    joined by muted ``→`` arrows and, by
    default, no hash is repeated after a name, so the compact form survives a 72-column
    row. The trace-flavoured options: ``show_hash`` annotates each named hop with the
    hash it is addressed by (``Alice (3d63)``), a ``None`` hop is our own device at a
    route's endpoints, and ``dim_from`` fades the tail a caller wants read as automatic
    (a boomerang's mirrored return leg). ``hash_as_name`` reframes an *unnamed* hop as
    its own identity — the hash at the path-hash-mode (``prefix_bytes``) width, muted
    grey with no prefix lit (colour is the "this is a name" signal, and there is no
    name), annotated with its addressed byte like a named hop (``e839f2 (e8)``) — where
    the default instead lights the compact addressed hash. An empty path reads as
    ``empty``.

    Args:
        hops: The hops in propagation order — hex hashes, with ``None`` marking our
            own device (empty strings are skipped; ``dim_from`` counts rendered hops).
        resolve: Maps a hop hash to a friendly name when known.
        prefix_bytes: Path-hash width an unnamed hop's identity hash presents at under
            ``hash_as_name`` (unnamed hops are otherwise shown grey whole, no prefix lit).
        self_name: Our own node's name — drawn in the white ``you`` style when a
            resolved name matches it, and naming any ``None`` device hop.
        empty: The muted text shown when there are no hops (e.g. ``"direct"``).
        show_hash: Annotate named hops (and, with ``device_hash``, our device) with
            their hash in parentheses — the trace presentation.
        hash_bytes: Truncate shown/annotated hashes to this byte width (the width the
            hops were addressed at); ``None`` shows them whole.
        device_hash: Our own device's key, annotated onto ``None`` hops when
            ``show_hash`` is on.
        dim_from: Render hops at/after this index — and the arrows into them — faint
            (a planned route's return leg); ``None`` dims nothing.
        hash_as_name: Present each unnamed hop as its identity hash — muted grey at the
            ``prefix_bytes`` width, annotated with its addressed byte (``hash_bytes``)
            like a named hop — rather than the compact prefix-lit addressed hash.

    Returns:
        A one-line :class:`Text`. Space tighter than the path is the caller's call,
        per surface: ellipsize (``no_wrap``), wrap under a hanging indent, or crop
        with horizontal scrolling.
    """
    shown = [h for h in hops if h is None or h]
    if not shown:
        return Text(empty, style="muted")
    text = Text()
    for i, hop in enumerate(shown):
        dim = dim_from is not None and i >= dim_from
        if i:
            text.append(" → ", style="faint" if dim else "muted")
        text.append_text(
            _path_node(
                hop,
                resolve,
                prefix_bytes=prefix_bytes,
                self_name=self_name,
                show_hash=show_hash,
                hash_bytes=hash_bytes,
                device_hash=device_hash,
                dim=dim,
                hash_as_name=hash_as_name,
            )
        )
    return text


def revisit_note(
    repeats: Sequence[str],
    resolve: NodeResolver = identity_label,
    *,
    prefix_bytes: int = 0,
    self_name: str | None = None,
) -> Text | None:
    """The warning a path that touches one hop twice owes its reader, or ``None`` for none.

    A route graph drawn with ``allow_duplicate_nodes``
    (:func:`~meshterm.ui.pathgraph.render_path_graph`) plants two markers carrying the same
    name, and the ``via`` row above it names the same node twice — both honest, both confusing
    left unexplained. This is the one line that explains them, and it refuses to pick between the
    two readings, because nothing in the packet can: an observed chain addresses its hops by a
    hash a single byte wide, so a repeat is as easily two different nodes colliding on that byte
    as one node the packet genuinely passed twice. It names the repeated hops through THE path
    widget (:func:`path_text`), so each reads in its own name hue exactly as it does in the
    ``via`` row the reader is comparing against.

    Takes the repeated hops rather than the path itself, so the caller decides what "repeated"
    means for its surface: one path asks :func:`~meshterm.ui.pathgraph.revisited_hops`, while a
    surface drawing a *fan* of paths must union each path's own revisits — a hop two different
    routes share is not a revisit at all, and pooling their hops would libel it as one.

    Args:
        repeats: The hops to warn about, already known repeated (empty = no warning).
        resolve: Maps a hop hash to a friendly name when known.
        prefix_bytes: Path-hash width to light in an unnamed hop's hash (0 = none).
        self_name: Our own node's name, so a repeat of *us* draws in the white ``you`` style.

    Returns:
        A one-line :class:`Text` fitting 72 cells for the paths a radio really carries, or
        ``None`` when ``repeats`` is empty and there is nothing to warn about.
    """
    if not repeats:
        return None
    # The mark carries its own span rather than riding the Text's base style, so nothing that
    # follows can inherit ``warn`` — the names keep their node hues and the prose stays muted.
    note = Text()
    note.append("⚠ ", style="warn")
    for i, hop in enumerate(repeats):
        if i:
            note.append(", ", style="muted")
        note.append_text(path_text([hop], resolve, prefix_bytes=prefix_bytes, self_name=self_name))
    note.append(" repeats — a loop, or two nodes sharing one hash", style="muted")
    return note


def _path_node(
    hop: str | None,
    resolve: NodeResolver,
    *,
    prefix_bytes: int,
    self_name: str | None,
    show_hash: bool,
    hash_bytes: int | None,
    device_hash: str | None,
    dim: bool,
    hash_as_name: bool = False,
) -> Text:
    """One node of :func:`path_text` (see there for the rendering rules)."""
    note_style = "faint" if dim else "muted"
    if hop is None:
        text = Text(self_name or LOCAL_DEVICE_LABEL, style="faint" if dim else "you")
        annotated = _shorten_hash(device_hash, hash_bytes) if device_hash else ""
        if show_hash and annotated:
            text.append(f" ({annotated})", style=note_style)
        return text
    named = resolve(hop)
    if named and named != hop:
        if dim:
            style = "faint"
        elif self_name and named == self_name:
            style = "you"
        else:
            style = name_style(named, hop)
        text = Text(named, style=style)
        if show_hash:
            text.append(f" ({_shorten_hash(hop, hash_bytes)})", style=note_style)
        return text
    shown = _shorten_hash(hop, hash_bytes)
    if dim:
        return Text(shown, style="faint")
    if hash_as_name:
        # No name: the hash is the node's identity. Show it at the path-hash-mode
        # width, fully muted (prefix_bytes=0 lights nothing — colour is the "this is
        # a name" signal, and there is no name), then annotate it with the byte it
        # was addressed by, exactly as a named hop is — unless that byte already *is*
        # the whole shown hash. So a 3-byte mode reads ``e839f2 (e8)`` and still
        # cross-references a byte-labelled route graph.
        identity = _shorten_hash(hop, prefix_bytes or None)
        text = highlighted_hash(identity, 0, known=False)
        if show_hash and shown and shown != identity:
            text.append(f" ({shown})", style=note_style)
        return text
    # No name resolved: the node is unknown, and an unknown node's hash is grey whole —
    # the prefix never lights (colour marks an identified node; see highlighted_hash).
    return highlighted_hash(shown, prefix_bytes, known=False)


def highlighted_hash(
    value: str, prefix_bytes: int, width: int | None = None, *, known: bool = True
) -> Text:
    """Render a hex key with its leading path-hash prefix highlighted — THE hash widget.

    The one way MeshTerm displays a hash, wherever one appears: the first
    ``prefix_bytes`` bytes are the slice other nodes address in a forced trace path
    (the path-hash), lit in the node's hash-derived palette hue — the same hue its
    name wears (see :func:`~meshterm.ui.theme.node_style`) — with the remainder muted,
    so the addressable prefix stands out within the otherwise full key. A ``width``
    budget shorter than the key ellipsizes it (the ``…`` takes the colour of the digit
    it replaces, so a highlight wider than the budget still reads as one).

    Colour marks an *identified* node (JP, 2026-08-08): a hash whose node nobody can
    name passes ``known=False`` and reads whole in the app-wide ``node.unknown`` grey —
    exactly as an unknown node draws in the path graph — never in a hue its bytes would
    derive. ``prefix_bytes=0`` on a known node likewise lights nothing and the key,
    standing in *as* a name, takes ``node.unknown`` rather than ``muted``: it is the
    row's content, not the dim tail of a hash whose head already carries the hue.

    Args:
        value: The key as hex, optionally ``0x``-prefixed and mixed-case.
        prefix_bytes: Number of leading bytes the current path-hash mode addresses; ``0``
            (or negative) leaves the whole key un-highlighted.
        width: Display budget in cells; a longer key is truncated to the whole leading
            bytes that fit in ``width - 1`` cells (an even digit count — a hash reads in
            bytes, two hex digits each) plus an ellipsis, a shorter one is right-padded to
            the budget so lanes stay aligned. ``None`` shows the key whole, unpadded.
        known: Whether the hash belongs to a node the caller can identify (name it, or
            place it as a real contact). ``False`` greys the whole run, prefix included.

    Returns:
        A styled :class:`Text` of the key (exactly ``width`` cells when given).
    """
    raw = value.lower().removeprefix("0x")
    split = max(0, prefix_bytes) * 2 if known else 0
    pad = 0
    ellipsis = False
    if width is not None and len(raw) > width:
        # Truncate on a byte boundary: keep an even number of hex digits, the ellipsis
        # taking the next cell and any odd cell left over padding out, so a mid-byte digit
        # never shows and the lane still spans exactly ``width``.
        kept = max(0, width - 1)
        kept -= kept % 2
        raw, ellipsis = raw[:kept], True
        pad = width - kept - 1
    elif width is not None:
        pad = width - len(raw)
    # The hue derives from the untruncated key (any prefix agrees) — but only an
    # identified node earns one at all; an unknown node's hash is grey throughout.
    hue = node_style(value) if known else "node.unknown"
    rest = "muted" if split else "node.unknown"
    text = Text()
    text.append(raw[:split], style=hue)
    text.append(raw[split:], style=rest)
    if ellipsis:
        text.append("…", style=hue if len(raw) < split else rest)
    text.append(" " * pad)
    return text


def name_chip(label: str, key: str | None = None, *, you: bool = False) -> Text:
    """A node name drawn as a path line's single chip — THE way a surface *frames* a name.

    Not an imitation of the chip language but a use of it: this builds a one-hop
    :class:`~meshterm.ui.pathline.PathLine` and asks it for its line, so the fill, the
    ink, the padding, the rounded cap and the closing point are all whatever a route's
    segments are wearing today, and can never drift from them (JP, 2026-08-12). The colour
    reads exactly as it does in a route — the *fill* carries the node's hue with dark ink
    on it, a node no key can place takes the keyless grey, and our own end is the map's
    ``★`` on its neutral dark grey rather than a name.

    Degrading is inherited too: where the terminal can't draw powerline separators, a path
    line is arrow-mode text and a lone hop is simply the name in its own hue — which is
    what a sender label was before it was framed, and what the PicoCalc console (whose font
    carries no separator glyph) goes on drawing.

    Args:
        label: The name as it should read (already resolved; this never renames it).
            Ignored when ``you`` — our own end is the star, never our name.
        key: Any known prefix of the node's key, which picks the hue. ``None`` is a node
            we can't place: the keyless grey, colour being reserved for keyed identities.
        you: This is our own node — the ``★``, exactly as a route draws our end of it.

    Returns:
        The chip as a one-line :class:`Text`.
    """
    hop = PathHop(SELF_GLYPH, you=True) if you else PathHop(label, key=key or None)
    return PathLine([hop]).text()


def _shorten_hash(value: str, hash_bytes: int | None) -> str:
    """Return ``value`` as bare hex truncated to ``hash_bytes`` bytes.

    Args:
        value: A hex hash, optionally ``0x``-prefixed and mixed-case.
        hash_bytes: Width in bytes to truncate to; the full value when falsy/unknown.

    Returns:
        Lowercase hex with no ``0x`` prefix, at most ``hash_bytes`` bytes wide.
    """
    raw = value.lower().removeprefix("0x")
    return raw[: hash_bytes * 2] if hash_bytes else raw


#: The pure-white our-own-node hue, matching the ``you`` style — an endpoint that is us
#: (and any relay resolving to our own name) takes it over its palette colour.
_SELF_RGB: RGB = (255, 255, 255)


def _name_rgb(name: str, key: str | None = None) -> RGB:
    """The RGB of a node's stable palette hue (``theme.name_style`` minus its bold).

    A name with no resolvable key styles ``node.unknown`` (a theme name, not a hex), so
    it lands on that grey here — the raster twin of the app-wide keyless-stays-grey rule.
    """
    hexpart = name_style(name, key).split()[-1]
    return parse_hex(hexpart) if hexpart.startswith("#") else mark_rgb(hexpart)


def name_rgb(name: str, key: str | None = None) -> RGB:
    """The truecolour of a node's stable palette hue, for a braille canvas.

    The public face of :func:`_name_rgb` — the same hue :func:`~meshterm.ui.theme.name_style`
    paints a name in (hash-derived when ``key`` is given), as an ``(r, g, b)`` tuple a
    raster can plot. A caller colouring node markers by their mesh identity (the trophy
    case's area drawing) mints them through here.
    """
    return _name_rgb(name, key)


#: Maps a relay hash to its node type (see the ``NODE_TYPE_*`` constants), or ``None`` when
#: the type is unknown — the hook that lets the route graph mark a repeater ``▲`` and not
#: just a generic ``●``. Endpoint sentinels are never passed to it.
TypeOf = Callable[[str], int | None]


def route_graph_style(
    *,
    resolve: NodeResolver,
    self_name: str | None,
    source: str | None,
    destination: str | None = None,
    type_of: TypeOf | None = None,
    key_of: NameKeyResolver | None = None,
) -> tuple[GlyphOf, LabelOf, LabelRgbOf]:
    """Build the per-node callbacks that draw a route on THE route graph (``pathgraph``).

    The shared presentation the Message paths dialog and the Trophy case both draw their
    graphs with: the two endpoints carry node names, every relay in between its marker
    plus the first byte of its hash, and each label takes its node's own name hue (us the
    pure-white ``you``) so a byte reads as the mesh name it stands for. It maps the graph's
    two endpoint sentinels — :data:`~meshterm.ui.pathgraph.SRC_NODE` on the left,
    :data:`~meshterm.ui.pathgraph.DST_NODE` on the right — plus every relay hash, to a
    glyph, a label, and a label colour.

    The right endpoint is *usually* us, and was once unconditionally so. That is right for
    a route we walked or a message we received, and wrong for one we sent: with our own
    name on both ends, every outgoing message's graph drew a round trip (JP, 2026-09-02).
    ``destination`` names it when it is somebody else.

    Args:
        resolve: Maps a relay's hash to a friendly name when one is known.
        self_name: Our own node's name — the right endpoint's label, drawn white.
        source: The left endpoint's display name (a message's origin, or us for a walk
            that starts at home — pass ``self_name`` to draw both ends as us). ``None``
            reads as an unknown ``?`` origin.
        destination: The right endpoint's display name, when the route does not end at us —
            the recipient of a message we sent. ``None`` (the default) draws us, which is
            what a walk and every received message want.
        type_of: Maps a relay's hash to its node type, so a relay draws its own map marker
            (``▲`` repeater, ``■`` room, ``◉`` sensor) in the shared palette instead of a
            generic dot. ``None``, or a hash whose type it can't resolve, keeps the old
            named-dot / unknown-ring fallback.
        key_of: Maps the ``source`` display name back to its node's key (see
            :func:`~meshterm.services.trace_runner.make_name_key_resolver`), so the left
            endpoint's label takes its key-derived hue; ``None``, or a name it can't
            place, leaves the origin muted — colour is reserved for keyed identities.

    Returns:
        The ``(glyph_of, label_of, label_rgb_of)`` triple to hand to
        :func:`~meshterm.ui.pathgraph.render_path_graph`.
    """
    src_is_self = bool(source) and source == self_name
    dst_is_self = not destination or destination == self_name

    def glyph_of(node: str) -> tuple[str, str]:
        """Us a star, a typed relay its map marker, a named node a dot, else a ring."""
        if node == DST_NODE:
            return SELF_MARK if dst_is_self else NODE_MARK
        if node == SRC_NODE:
            return SELF_MARK if src_is_self else (NODE_MARK if source else UNKNOWN_MARK)
        if type_of is not None:
            node_type = type_of(node)
            if node_type is not None:
                return NODE_GLYPHS.get(node_type, DEFAULT_GLYPH)
        named = resolve(node)
        return NODE_MARK if named and named != node else UNKNOWN_MARK

    def label_of(node: str) -> str | None:
        """Endpoints by name, relays by their first hash byte."""
        if node == DST_NODE:
            return (self_name or "you") if dst_is_self else destination
        if node == SRC_NODE:
            return source or "?"
        return node[:2]

    def label_rgb_of(node: str) -> RGB:
        """A label's colour: its node's name hue, us pure white, an unknown its ring.

        A relay we can't name keeps its *marker's* colour, so the hash under a repeater's
        ``▲`` reads as that repeater rather than as a stray grey byte — which means this
        resolves whatever ``glyph_of`` hands back, style name or hex alike.
        """
        if node == DST_NODE:
            if dst_is_self:
                return _SELF_RGB
            return _name_rgb(destination, key_of(destination) if key_of else None)
        if node == SRC_NODE:
            if src_is_self:
                return _SELF_RGB
            if not source:
                return mark_rgb(UNKNOWN_MARK[1])
            return _name_rgb(source, key_of(source) if key_of else None)
        named = resolve(node)
        if named and named != node:
            return _name_rgb(named, node)
        return mark_rgb(glyph_of(node)[1])

    return glyph_of, label_of, label_rgb_of


def route_path(
    result: TraceResult,
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = identity_label,
    device_hash: str | None = None,
    *,
    bare_self: bool = False,
    show_hash: bool = True,
) -> PathLine:
    """A trace's walked route as THE path widget's line object.

    The same route :func:`_route_text` renders — this hands back the
    :class:`~meshterm.ui.pathline.PathLine` itself, so a surface with room to spare
    can wrap it at hop boundaries (:meth:`~meshterm.ui.pathline.PathLine.wrapped`)
    rather than taking the one-liner and folding it mid-name.

    Args:
        result: The trace whose route to build.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        device_hash: Our own device's key/hash, annotated onto its endpoints when known.
        bare_self: Draw both ``us`` endpoints as their bare arrow — for a surface (the
            trace screens' route lane) where a walk starting and ending on us is the
            premise, not news.
        show_hash: Annotate each *named* hop with the hash it was addressed by. On for
            the scripted table output, where the hash is half the answer; off for the
            live screens, whose route lane reads as the sequence of nodes and leaves
            the hex to the wire-spec lane below it. Unnamed hops always show their
            hash — it is the only identity they have.

    Returns:
        The route's :class:`~meshterm.ui.pathline.PathLine` (hopless — reading
        ``no hops recorded`` — when the trace recorded none).
    """
    edges = result.edges(device_label)
    if not edges:
        return PathLine([], empty="no hops recorded")
    hash_bytes = result.path_hash_bytes
    nodes = [edges[0].origin] + [edge.destination for edge in edges]
    hops = [None if not node or node == device_label else node for node in nodes]
    return path_line(
        hops,
        resolve,
        prefix_bytes=hash_bytes or 8,
        self_name=device_label,
        show_hash=show_hash,
        hash_bytes=hash_bytes,
        device_hash=device_hash,
        bare_self=bare_self,
    )


def _route_text(
    result: TraceResult,
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = identity_label,
    device_hash: str | None = None,
) -> Text:
    """Render a trace's walked route through THE path widget, on one line.

    Shows the path the trace actually walked — the forced path, or the route the
    device resolved when auto-routing — in the trace presentation: each node
    annotated by its hash at the command's path-hash width, our own device
    bracketing both ends, e.g.
    ``Me (a1b2) → Alice (3d63) → Bob (f2a1) → Me (a1b2)``.

    Args:
        result: The trace whose route to display.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        device_hash: Our own device's key/hash, annotated onto its endpoints when known.

    Returns:
        A :class:`Text` with the node sequence, or a muted note when no hops exist.
    """
    return route_path(result, device_label, resolve, device_hash).text()


def _hop_medians_table(
    hop_snrs: list[HopAggregate],
    device_label: str,
    resolve: NodeResolver = identity_label,
    hash_bytes: int | None = None,
    device_hash: str | None = None,
) -> Table:
    """Render the per-hop median SNR aggregated across a run's traces.

    Args:
        hop_snrs: The per-hop aggregates to display.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        hash_bytes: Path-hash width (bytes) to truncate shown node hashes to.
        device_hash: Our own device's key/hash, annotated onto the path's endpoints.

    Returns:
        A compact Rich :class:`Table` of hop, link, and median SNR.
    """
    table = Table(box=None, padding=(0, 1, 0, 0), expand=False)
    table.add_column("HOP", justify="right", style="muted")
    table.add_column("FROM → TO")
    table.add_column("MEDIAN SNR", justify="right")
    for agg in hop_snrs:
        table.add_row(
            str(agg.index),
            link_text(agg.origin, agg.destination, device_label, resolve, hash_bytes, device_hash),
            Text(f"{agg.median_snr:+.1f} dB", style=snr_style(agg.median_snr)),
        )
    return table


def stats_panel(
    stats: TraceStats,
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = identity_label,
    route: TraceResult | None = None,
    device_hash: str | None = None,
) -> Panel:
    """Summarize aggregated trace statistics in a panel.

    Includes the median SNR for every hop along the path (not just the bottleneck), so
    a weak link anywhere in the route is visible. When a representative ``route`` trace
    is given, the path it actually walked is shown as a node sequence, e.g.
    ``Me → Alice → Bob → Me``.

    Args:
        stats: The aggregated statistics to display.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        route: A representative trace whose walked route to display, if any.
        device_hash: Our own device's key/hash, annotated onto the route endpoints.

    Returns:
        A Rich :class:`Panel` with the route, success rate, robust SNR/RTT, and
        per-hop medians.
    """
    snr = stats.median_min_snr
    snr_text = Text(f"{snr:+.1f} dB", style=snr_style(snr)) if snr is not None else Text("n/a")
    rtt = f"{stats.median_rtt_ms:.0f} ms" if stats.median_rtt_ms is not None else "n/a"
    summary = Text.assemble(
        ("target        ", "muted"),
        (f"{stats.target}\n", ""),
        ("success rate  ", "muted"),
        (f"{stats.success_rate:.0%} ({stats.successes}/{stats.samples})\n", ""),
        ("median min SNR ", "muted"),
        snr_text,
        ("\n", ""),
        ("median RTT    ", "muted"),
        (rtt, ""),
    )
    sections: list[Text | Table] = []
    if route is not None:
        sections.append(Text("Route", style="accent"))
        sections.append(_route_text(route, device_label, resolve, device_hash))
        sections.append(Text())  # blank line before the stats block
    sections.append(summary)
    if stats.hop_snrs:
        sections.append(Text("\nPer-hop medians", style="accent"))
        hash_bytes = route.path_hash_bytes if route is not None else None
        sections.append(
            _hop_medians_table(stats.hop_snrs, device_label, resolve, hash_bytes, device_hash)
        )
    body: Text | Group = sections[0] if len(sections) == 1 else Group(*sections)
    return Panel(body, title="[accent]Trace summary[/accent]", border_style="accent", expand=False)


# Node-type glyphs and their colours, consistent with the map's marker palette across the
# whole app (see ui.map_render): our own node is the yellow ``★``, plain nodes the loud pink
# ``●`` and repeaters the calmer violet ``▲``. The remaining types take map-safe hues that
# stay distinct from those — a white square for rooms (the house glyph read poorly) and an
# orange ringed dot for sensors. All are single-width BMP glyphs so columns stay aligned.
# The colours are the theme's ``type.*`` entries rather than raw hex, so the 16-slot console
# picks its slot deliberately (the violet would otherwise downsample to grey, and a repeater
# would read as an unknown node); on the regular platform they *are* the map's hues.
NODE_GLYPHS: dict[int, tuple[str, str]] = {
    NODE_TYPE_REPEATER: (REPEATER_MARK[0], "type.repeater"),
    NODE_TYPE_ROOM: ("■", "type.room"),
    NODE_TYPE_SENSOR: ("◉", "type.sensor"),
    NODE_TYPE_CHAT: (NODE_MARK[0], "type.node"),
}
DEFAULT_GLYPH: tuple[str, str] = (NODE_MARK[0], "type.node")


def node_marker(node_type: int | None) -> tuple[str, RGB]:
    """The map-palette glyph and colour for a node type, as a ``(glyph, rgb)`` pair.

    The shared node-type marks (``▲`` repeater, ``■`` room, ``◉`` sensor, ``●`` plain
    node) in the map's own colours, minted here for a braille raster the way
    :data:`NODE_GLYPHS` mints them for a Rich row — so a spatial drawing pins its nodes
    in the exact glyphs and hues the map and the nodes list use. Both read the same
    ``type.*`` theme entry (through :func:`~meshterm.ui.theme.mark_rgb` here), so the
    raster and the row agree on whatever the platform's palette can afford. An unknown
    type falls back to the plain node mark.
    """
    glyph, style = NODE_GLYPHS.get(node_type or -1, DEFAULT_GLYPH)
    return glyph, mark_rgb(style)


def self_marker() -> tuple[str, RGB]:
    """Our own node's map marker — the yellow ``★`` — as a ``(glyph, rgb)`` pair."""
    return SELF_MARK[0], parse_hex(SELF_MARK[1])


# A heat-map gradient for a node's heard age, hottest (most recently heard) to coldest: white
# → yellow → orange → red → grey. Each stop pairs an age anchor (log10 of seconds since heard)
# with an RGB colour; :func:`_recency_style` interpolates continuously between them, so the
# colour glides with recency rather than snapping between a handful of discrete shades.
_HEAT_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (math.log10(300), (255, 255, 255)),  # ≤5m — white (fresh)
    (math.log10(3600), (250, 204, 21)),  # ~1h  — yellow
    (math.log10(21600), (251, 146, 60)),  # ~6h  — orange
    (math.log10(86400), (248, 113, 113)),  # ~1d  — red
    (math.log10(604800), (148, 163, 184)),  # ~1w  — grey
    (math.log10(2592000), (100, 116, 139)),  # ~30d+ — cold slate
)
_RECENCY_NEVER = "#64748b"  # never heard — the coldest slate


def age_seconds(when: datetime | None) -> float | None:
    """Seconds since ``when`` (aware UTC), or ``None`` when unknown/naive."""
    if when is None or getattr(when, "tzinfo", None) is None:
        return None
    return max(0.0, (utcnow() - when).total_seconds())


def format_age(secs: float | None) -> str:
    """A compact relative age — ``now``, ``5m``, ``3h``, ``2d``, ``4w`` — or ``never``."""
    if secs is None:
        return "never"
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    if secs < 604800:
        return f"{int(secs // 86400)}d"
    return f"{int(secs // 604800)}w"


def body_heading(title: str, note: str = "") -> Text:
    """A section heading inside a screen's body: accent title, optional muted ``  ·  note``.

    THE form for a heading that sits *in* a page's prose or drawings (the Time Machine's
    Volume / SNR / Rhythm, a record's Stats / Area walked / Route), as opposed to a grouped
    list's ``── Label ──`` landmark (:func:`~meshterm.ui.menus.section_heading`), which
    pins and is jumped to. The note reads the block back — its unit, its window, what its
    labels mean — and is muted so the title stays the landmark.

    Args:
        title: The section's name, sentence case.
        note: An aside after the roomy separator, or ``""`` for none.

    Returns:
        The heading as one styled line.
    """
    text = Text(title, style="accent")
    if note:
        text.append(f"  ·  {note}", style="muted")
    return text


def format_ago(secs: float | None) -> str:
    """The relative-age *phrase* — ``now``, ``5m ago``, ``never`` — for running prose.

    The canonical grammar for every "heard … (…)" and "delivered …" row: a fresh
    sighting reads as bare ``now`` and an unknown one as bare ``never`` (neither takes
    the suffix — "now ago" is nonsense), while any measured age reads ``5m ago``.
    Callers embedding an age in a sentence or parenthetical use this;
    :func:`format_age` stays the bare column form for aligned age lanes.
    """
    age = format_age(secs)
    return age if age in ("now", "never") else f"{age} ago"


def _recency_style(secs: float | None) -> str:
    """The heat-map colour for a node's heard age of ``secs`` (hotter = more recent).

    Dispatches to the platform-bound implementation: the regular platform's continuous
    gradient, or PicoCalc's quantized steps (a 16-slot palette has no room to glide).
    """
    return _recency_impl(secs)


def _recency_gradient(secs: float | None) -> str:
    """The regular platform's heat: continuous interpolation over :data:`_HEAT_STOPS`.

    Interpolates the RGB channels between the two stops bracketing ``secs`` (in log-age
    space), clamping to white below the first stop and cold slate above the last.
    """
    if secs is None:
        return _RECENCY_NEVER
    x = math.log10(max(secs, 0.0) + 1.0)
    if x <= _HEAT_STOPS[0][0]:
        r, g, b = _HEAT_STOPS[0][1]
    elif x >= _HEAT_STOPS[-1][0]:
        r, g, b = _HEAT_STOPS[-1][1]
    else:
        (x0, c0), (x1, c1) = next(
            (lo, hi) for lo, hi in pairwise(_HEAT_STOPS) if lo[0] <= x <= hi[0]
        )
        f = (x - x0) / (x1 - x0)
        r, g, b = (round(a + (bb - a) * f) for a, bb in zip(c0, c1, strict=True))
    return f"#{r:02x}{g:02x}{b:02x}"


#: The heat ladder as ``(younger than, style)``, hottest first — JP's spec. Every boundary
#: is a plain human unit, so a colour change always lands on a number you can say out loud
#: ("under five minutes", "over a week"). Past a year nothing is worth distinguishing from
#: never heard, so the two share the coldest step.
_HEAT_STEPS: tuple[tuple[float, str], ...] = (
    (300, "heat.now"),  # under 5 minutes — white
    (3600, "heat.minutes"),  # 5 minutes       — yellow
    (86400, "heat.hours"),  # 1 hour          — light red
    (604800, "heat.days"),  # 1 day           — brown
    (2592000, "heat.weeks"),  # 1 week          — red
    (31536000, "heat.months"),  # 1 month         — light grey
)  # 1 year / never  — dark grey (heat.never)


def _recency_quantized(secs: float | None) -> str:
    """PicoCalc's heat: the gradient stepped onto the theme's ``heat.*`` rungs.

    Same scale as the regular platform's gradient and the same anchors — a console that
    can't spend a hue per second spends one per unit instead (see :data:`_HEAT_STEPS`).
    """
    if secs is None:
        return "heat.never"
    for limit, style in _HEAT_STEPS:
        if secs < limit:
            return style
    return "heat.never"


_recency_impl: Callable[[float | None], str] = _recency_gradient


@on_platform
def _bind_heat(platform: Platform) -> None:
    """Pick the heat implementation for the platform (runs now and on every switch)."""
    global _recency_impl
    _recency_impl = _recency_gradient if platform.truecolor else _recency_quantized


def _key_id(value: str) -> str:
    """Normalise a key/prefix to the lowercased 12-hex id used to match heard nodes."""
    return value.lower().removeprefix("0x")[:12]


def contact_packets(contact: Contact, counts: dict[str, int]) -> int | None:
    """The overheard-packet tally for ``contact``, or ``None`` if never overheard."""
    ident = contact.public_key or contact.key_prefix
    return counts.get(_key_id(ident)) if ident else None


# The columns the contact list can be sorted by, left-to-right, and the direction each opens on
# — name A→Z, most-recently-heard first, most packets first — chosen so a fresh sort shows
# the "interesting" end at the top.
_SORT_COLUMNS: tuple[str, ...] = ("name", "heard", "packets")
_SORT_OPENS_ASCENDING: dict[str, bool] = {"name": True, "heard": True, "packets": False}


@dataclass
class ContactsSort:
    """Which column the contact list is sorted by, and in which direction.

    ``column`` is one of :attr:`columns`; ``ascending`` sorts the column's underlying
    metric low-to-high — name A→Z, *age* (so ascending = most recently heard first), packet
    count low-to-high. The interactive screen mutates this in place as the user presses the
    arrows.

    The sort *ring* is per-instance: :attr:`columns` and :attr:`opens_ascending` default to
    the Contacts list's three (:data:`_SORT_COLUMNS`), but the Time Machine picker passes a
    wider set — it adds a sortable ``hash`` column — so the same model drives both without a
    module-global column list that one screen would have to share with the other.
    """

    column: str = "name"
    ascending: bool = True
    columns: tuple[str, ...] = _SORT_COLUMNS
    opens_ascending: dict[str, bool] = field(default_factory=lambda: _SORT_OPENS_ASCENDING)

    @classmethod
    def names(cls, columns: tuple[str, ...] = _SORT_COLUMNS) -> tuple[str, ...]:
        """The orders :meth:`from_name` accepts — what a caller may legitimately ask for.

        :meth:`from_name` answers "which sort is this?" and forgives anything it does not
        recognise. A surface that wants to *refuse* an unknown order needs the set itself,
        which is this.
        """
        return columns

    @classmethod
    def from_name(
        cls,
        name: str,
        columns: tuple[str, ...] = _SORT_COLUMNS,
        opens_ascending: dict[str, bool] | None = None,
    ) -> ContactsSort:
        """Build a sort for ``name`` over ``columns``, opening in that column's natural direction.

        Falls back to the ring's first column when ``name`` isn't one of ``columns`` (so a
        stale saved key can't wedge the sort). ``opens_ascending`` defaults to the Nodes
        list's directions when omitted.
        """
        opens = opens_ascending if opens_ascending is not None else _SORT_OPENS_ASCENDING
        column = name if name in columns else columns[0]
        return cls(column, opens[column], columns, opens)

    def move(self, delta: int) -> None:
        """Step the active column ``delta`` places (wrapping), adopting its natural direction."""
        index = (self.columns.index(self.column) + delta) % len(self.columns)
        self.column = self.columns[index]
        self.ascending = self.opens_ascending[self.column]


def ordered_contacts(
    contacts: list[Contact], counts: dict[str, int], sort: ContactsSort
) -> list[Contact]:
    """Contacts sorted per ``sort`` (our own node is pinned separately, above these).

    The active column's metric drives the order (reversed for a descending sort); ties always
    break by case-folded name *ascending*, so two contacts sharing a metric (e.g. the same
    ``heard`` age) keep a stable A→Z order instead of flipping with the primary direction.
    Never-heard / never-overheard rows carry an extreme metric so they gather at the ascending
    end.
    """
    if sort.column == "heard":
        # One clock snapshot for the whole sort: per-row utcnow() calls would skew two
        # identical last_seen stamps apart by microseconds and defeat the name tie-break.
        now = utcnow()

        def metric(c: Contact) -> float:
            if c.last_seen is None or getattr(c.last_seen, "tzinfo", None) is None:
                return float("inf")
            return max(0.0, (now - c.last_seen).total_seconds())
    elif sort.column == "packets":

        def metric(c: Contact) -> float:
            return contact_packets(c, counts) or 0
    else:

        def metric(c: Contact) -> object:
            return c.name.casefold()

    # Name-ascending first, then a stable sort by the primary metric: equal-metric rows retain
    # their A→Z order under both directions (reversing the whole list would flip the tiebreak).
    ordered = sorted(contacts, key=lambda c: c.name.casefold())
    ordered.sort(key=metric, reverse=not sort.ascending)
    return ordered


def _sort_header(label: str, column: str, sort: ContactsSort) -> str:
    """A column header: plain-muted, or lit with a direction triangle when it's the sort key.

    The active column's name and its triangle are lit together in the app's ``cursor`` white
    (so the interactive left/right selection is obvious) — ``▲`` for ascending, ``▼`` for
    descending. Same ink as the highlighted *row*, because it is the same claim one axis
    over. The column reserves its width (see :func:`contacts_table`) so toggling the sort
    doesn't shift the row.
    """
    if column != sort.column:
        return label
    triangle = "▲" if sort.ascending else "▼"
    return f"[cursor]{label} {triangle}[/]"


def node_type_legend(indent: str = "", width: int | None = None) -> Text:
    """The key to the node-type marks: ``★ you   ▲ repeater   ● companion   …``.

    Every glyph in its shared map colour (see :data:`NODE_GLYPHS`), each named muted after
    it. THE legend for any surface that draws typed node markers — the contacts list under its
    table, the route graph under its lanes — so one glyph means one thing app-wide.

    One line where it fits. Where it does not — the full names run to 59 cells, and the
    PicoCalc has 53 — it breaks onto a second line between entries, never inside one, so
    a mark is never parted from its name and no name is cropped.

    Args:
        indent: Leading spaces to sit the legend under a table or graph body.
        width: The cells available. None keeps the legend on one line.
    """
    entries = [Text.assemble((SELF_MARK[0], SELF_MARK[1]), (" you", "muted"))]
    for node_type in (NODE_TYPE_REPEATER, NODE_TYPE_CHAT, NODE_TYPE_ROOM, NODE_TYPE_SENSOR):
        glyph, color = NODE_GLYPHS[node_type]
        entries.append(Text.assemble((glyph, color), (f" {NODE_TYPE_LABELS[node_type]}", "muted")))

    gap = "   "
    legend = Text(indent)
    line = cell_len(indent)
    for i, entry in enumerate(entries):
        size = cell_len(entry.plain)
        if i and width is not None and line + len(gap) + size > width:
            legend.append("\n" + indent)
            line = cell_len(indent)
        elif i:
            legend.append(gap)
            line += len(gap)
        legend.append_text(entry)
        line += size
    return legend


#: Blank rows of air around a tab strip — one above and one under on the desktop; none
#: on the PicoCalc (JP, 2026-08-08), where rows are the scarce resource and both lines go
#: to the stage instead (the route graph's ceiling, the location preview's growth room).
#: Rides the same frugality signal as the borderless frame, and is bound at
#: platform-switch time like every platform-derived constant — never branched per paint.
_TAB_AIR = 1


@on_platform
def _bind_tab_air(platform: Platform) -> None:
    """Bind the strip's air to the platform (runs now and on every switch)."""
    global _TAB_AIR
    _TAB_AIR = 1 if platform.frame_border else 0


def tab_air() -> int:
    """Blank rows a tabbed page spends above its strip and under it (see :data:`_TAB_AIR`).

    Read by every screen that draws :func:`tab_strip` — the node page, a record — so the
    strip sits in the same air everywhere, and loses it on the same platform.
    """
    return _TAB_AIR


def tab_strip(labels: Sequence[str], active: int, width: int) -> Group:
    """A boxed tab strip: every tab always boxed on top, the active one open into the page.

    THE navigation header for a screen that pages one full-height view at a time between a
    handful of named views (the node detail page's ``Info`` / ``Routes``) instead of stacking
    them. Every tab is always drawn as a full-topped box — a dim ``faint`` outline by
    default, lit ``accent`` when active — instead of only the active one; this keeps every
    tab's width fixed regardless of which is selected, so switching tabs never shifts the
    labels that come after it. Tabs share a vertical between neighbours (one glyph, not
    two), and each shared *top* corner is drawn as if the tab to the left sits on top of the
    tab to the right: it "opens" (rounds toward the tab starting there) only at the very
    first tab and at the active tab's own left edge, and "closes" (rounds back, ceding the
    position to the tab on its left) everywhere else — so the active tab is the one exception
    that always wins both of its top corners, reading as lifted in front of its neighbours.

    The *bottom* border is a single continuous accent-coloured rule — it's one line, so it
    carries one colour throughout, the active tab's — that the inactive tabs sit flush
    against: they get no corners or seams of their own down there, just the rule passing
    behind them, broken only under the active tab, which is why it reads as merged into the
    page below it: the rule turns up into ``╯`` (closing off the flat run from the west,
    turning north into the tab's wall), leaves the active tab's width open (no line — that
    gap *is* the page starting), then turns back down through ``╰`` (the wall meeting the
    flat run continuing east) and carries on flat. A lone tab collapses to the plain accent
    heading (:func:`~meshterm.ui.menus.section_heading`'s ``── Label ──`` form) since there
    is nothing to switch between or layer. When there's slack in ``width``, the tab boxes
    (top and label rows) sit two columns in from the left; the bottom rule is one continuous
    line regardless, so it fills that margin rather than being indented with them. The
    owning screen switches the active index (``Tab``/``Shift+Tab``); the strip itself is
    pure presentation.

    Args:
        labels: The tab names in display order.
        active: Index of the lit tab.
        width: The strip's render width — the closing rule fills out to it.

    Returns:
        A :class:`~rich.console.Group` of one line (a lone tab, or none) or three (the shared
        top border, the boxed labels, and the shared bottom rule).
    """
    if not labels:
        return Group(Text(""))
    if len(labels) == 1:
        return Group(Text(f"── {labels[0]} ──", style="accent", no_wrap=True))

    n = len(labels)
    inner = [f"  {label}  " for label in labels]
    content_width = sum(len(cell) for cell in inner) + (n + 1)
    margin = 2 if content_width + 2 <= width else 0
    pad = " " * margin

    def top_owner(junction: int) -> int:
        """Which tab's top corner glyph sits at this junction — see the docstring's rule."""
        return junction if junction == 0 or junction == active else junction - 1

    def top_style(junction: int) -> str:
        return "accent" if top_owner(junction) == active else "faint"

    def bot_glyph(junction: int) -> str:
        """The bottom rule's glyph at this junction.

        A corner turning into or out of the active tab's wall, else a flat
        pass-through. Always accent — see the enclosing docstring for why.
        """
        if junction == active:
            return "╯"
        if junction == active + 1:
            return "╰"
        return "─"

    top = Text(pad, no_wrap=True)
    mid = Text(pad, no_wrap=True)
    bot = Text("─" * margin, style="accent", no_wrap=True)
    for i, _label in enumerate(labels):
        opens = i == 0 or i == active
        top.append("╭" if opens else "╮", style=top_style(i))
        mid.append("│", style=top_style(i))
        bot.append(bot_glyph(i), style="accent")
        tab_style = "accent" if i == active else "faint"
        top.append("─" * len(inner[i]), style=tab_style)
        mid.append(inner[i], style=tab_style)
        bot.append(" " * len(inner[i]) if i == active else "─" * len(inner[i]), style="accent")
    top.append("╮", style=top_style(n))
    mid.append("│", style=top_style(n))
    bot.append(bot_glyph(n), style="accent")
    bot.append("─" * max(0, width - len(bot.plain)), style="accent")
    return Group(top, mid, bot)


def _contacts_legend() -> Text:
    """The node-type legend, indented to sit under the contacts table body."""
    return node_type_legend(indent="  ")


def contacts_table(
    self_name: str,
    self_key: str,
    contacts: list[Contact],
    prefix_bytes: int,
    counts: dict[str, int],
    sort: ContactsSort | None = None,
) -> Group:
    """List this node and its known contacts with recency, packets, type, key, and legend.

    Our own node is the first row (``★``, name in the white ``you`` style); contacts follow
    in ``sort`` order.
    A per-type glyph marks each node in the app's shared colours, the name takes the node's
    hash-derived palette hue, the heard age glows with recency heat (brighter = fresher),
    and the full key is shown with its path-hash prefix lit — chopped with an ellipsis only
    when the terminal is too narrow.

    Args:
        self_name: This node's advertised name.
        self_key: This node's full public key (hex); blank renders as ``?``.
        contacts: Known contacts, listed after our own node.
        prefix_bytes: Path-hash width in bytes to highlight in every key.
        counts: Overheard-packet counts keyed by lowercased 12-hex node id (from
            monitoring); a contact with no entry shows ``—``.
        sort: The active sort (column + direction); defaults to name-ascending. The sorted
            column's header is lit cyan with an up/down direction triangle.

    Returns:
        A Rich :class:`Group` of the frameless table and its glyph legend.
    """
    sort = sort if sort is not None else ContactsSort()

    # expand=True lets the key column (the only flexible one) soak up all spare width and be
    # the sole column Rich squeezes when narrow — the fixed columns keep their natural size.
    table = Table(
        title=f"[accent]Contacts[/accent]  [muted]· {len(contacts)} known[/muted]",
        title_justify="left",
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        header_style="muted",
        expand=True,
        padding=(0, 2, 0, 0),
    )
    # Each sortable header reserves two extra columns for its " ▲" direction marker (via
    # min_width = label + 2) so the same width holds whether or not it's the active sort —
    # switching the sort never widens a column and shifts the rest of the row.
    table.add_column("", no_wrap=True)  # node-type glyph
    table.add_column(_sort_header("NAME", "name", sort), no_wrap=True, min_width=6)
    table.add_column(
        _sort_header("HEARD", "heard", sort), justify="right", no_wrap=True, min_width=7
    )
    table.add_column(
        _sort_header("PKTS", "packets", sort), justify="right", no_wrap=True, min_width=6
    )
    # The full key, chopped to an ellipsis by Rich only when the row won't otherwise fit.
    table.add_column("KEY", no_wrap=True, overflow="ellipsis", ratio=1, min_width=10)

    unknown = Text("?", style="muted")
    table.add_row(
        Text(SELF_MARK[0], style=SELF_MARK[1]),
        # Our own name is the app-wide pure-white "you" style, never a palette hue.
        Text.assemble((self_name, "you"), ("  (you)", "muted")),
        Text("—", style="faint"),
        Text("—", style="faint"),
        highlighted_hash(self_key, prefix_bytes) if self_key else unknown,
    )
    table.add_section()
    for c in ordered_contacts(contacts, counts, sort):
        secs = age_seconds(c.last_seen)
        glyph, glyph_style = NODE_GLYPHS.get(c.node_type, DEFAULT_GLYPH)
        pkts = contact_packets(c, counts)
        table.add_row(
            Text(glyph, style=glyph_style),
            Text(c.name, style=name_style(c.name, c.public_key or c.key_prefix)),
            Text(format_age(secs), style=_recency_style(secs)),
            Text(str(pkts), style="muted") if pkts else Text("—", style="faint"),
            highlighted_hash(c.public_key, prefix_bytes) if c.public_key else unknown,
        )
    return Group(table, Text(""), _contacts_legend())


def tx_opt_table(result: TxOptResult) -> Table:
    """Render every measured TX level of an optimization sweep, best row highlighted.

    Args:
        result: The optimization result to display.

    Returns:
        A Rich :class:`Table` of TX level, target SNR, success rate, and sample count.
    """
    table = Table(
        title=f"TX sweep · {result.admin_node} → {result.target}",
        border_style="muted",
        expand=False,
    )
    table.add_column("TX", justify="right")
    table.add_column("TARGET SNR", justify="right")
    table.add_column("SUCCESS", justify="right")
    table.add_column("TRACES", justify="right")
    for lv in result.sorted_by_tx():
        is_best = lv.tx_power == result.best_tx
        marker = "[ok]★[/ok] " if is_best else "  "
        snr = lv.target_snr
        snr_cell = Text(f"{snr:+.1f}", style=snr_style(snr)) if snr is not None else Text("—")
        # Highlight anything short of a perfect success rate — reliability comes first.
        rate = lv.success_rate
        rate_style = "ok" if rate >= 1.0 else ("warn" if rate > 0 else "err")
        rate_cell = Text(f"{rate:.0%}", style=rate_style)
        tx_cell = f"{marker}{lv.tx_power}"
        row_style = "ok" if is_best else None
        table.add_row(tx_cell, snr_cell, rate_cell, f"{lv.successes}/{lv.samples}", style=row_style)
    return table


def tx_opt_summary(result: TxOptResult) -> Panel:
    """Summarize a TX optimization outcome.

    Args:
        result: The optimization result to summarize.

    Returns:
        A Rich :class:`Panel` stating the chosen optimum and whether it was applied.
    """
    snr = result.best_snr
    snr_text = Text(f"{snr:+.1f} dB", style=snr_style(snr)) if snr is not None else Text("n/a")
    applied = (
        f"[ok]applied to {result.admin_node}[/ok]"
        if result.applied
        else "[muted]not applied[/muted]"
    )
    body = Text.assemble(
        ("tuning node   ", "muted"),
        (f"{result.admin_node}\n", "brand"),
        ("target        ", "muted"),
        (f"{result.target}\n", "brand"),
        ("optimal TX    ", "muted"),
        (f"{result.best_tx}", "brand"),
        ("\n", ""),
        ("target SNR    ", "muted"),
        snr_text,
        ("\n", ""),
        ("reliability   ", "muted"),
        (f"{result.best_success_rate:.0%}\n", ""),
        ("previous TX   ", "muted"),
        (f"{result.original_tx if result.original_tx is not None else '?'}\n", ""),
        ("status        ", "muted"),
        Text.from_markup(applied),
    )
    return Panel(
        body, title="[accent]TX optimization[/accent]", border_style="accent", expand=False
    )


# -- battery gauge --------------------------------------------------------------------------

#: Unicode braille pattern base; add an 8-bit dot mask (a 2×4 cell) to get the glyph.
_BRAILLE_BASE = 0x2800

#: Dot bit per (column, row) within a braille cell — the Unicode standard layout, the same
#: mapping the map canvas rasters with. Kept here so the one-cell battery glyph needn't reach
#: into the canvas module for two constants.
_BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))

#: The fuel-gauge bands: ``(percent floor, style)``, highest first. Colour answers *how much
#: pack is left* in three broad steps — light green on green over half, yellow on brown over a
#: quarter, light red on red below — while the eight-rung braille fill inside the block
#: answers *how much more precisely*. Two questions, two channels: a band that stayed put
#: while the dots climbed is what makes the cell readable at a glance and still exact on a
#: second look.
_BATTERY_BANDS: tuple[tuple[int, str], ...] = (
    (50, "batt.full"),
    (25, "batt.mid"),
    (0, "batt.low"),
)

#: Steps in the fill: one braille cell is 2×4 dots, and each dot row lights left-then-right,
#: so the cell reads as an eight-rung ladder — one rung per 12.5% of charge.
_BATTERY_STEPS = 8

#: Halfway down the last rung the cell starts alarming, and it does it by dropping the ground
#: rather than by changing hue: black dots on red one frame (``batt.flash``), the light red on
#: the bare page the next (``batt.flash.off``). Every other cell in the app's gauge is a
#: filled block, so a beat with no fill at all is the loudest thing the palette can say.
#: A percentage rather than a rung because half a rung is not a rung: a rung is ``100 / 8`` =
#: 12.5% of the pack, so its midpoint is 6.25% — the last dot half spent.
#:
#: The alternation is ours, drawn a frame at a time. The console's own blink attribute is no
#: use even where a terminal honours it: SGR 5 toggles the *foreground* and leaves the ground
#: alone, which is the half of this that matters (and the PicoCalc's framebuffer console
#: ignores it outright — device-checked, along with its want of bright backgrounds).
_BATTERY_FLASH_PCT = 100 / _BATTERY_STEPS / 2

#: Frames in the charging sweep: empty → four fills → round again (bottom-to-full loop).
_CHARGE_FRAMES = 5

#: A full pack reads ``100``, with no ``%`` — the one charge whose sign is dropped, so the
#: reading is three cells wide exactly as ``99%`` is.
#:
#: Which matters because 99 and 100 are the two numbers a topped-off pack sits *between*:
#: the charger cuts out at full, the pack settles back to 99, the charger restarts, and the
#: companion reports the flip every poll for as long as it is plugged in (JP, 2026-09-09).
#: The gauge is pinned to the header's right edge and the pulse takes what is left, so a
#: number that grew a cell at the top of the range took that cell off the sparkline and
#: redrew the whole activity history at a new scale, twice a minute, on a device sitting
#: still. Every other width change in the range — 9 to 10 on the way down — is a boundary a
#: discharging pack crosses once and never comes back over, so it costs one repaint and
#: needs no answer.
#:
#: A bare ``100`` beside a full block reads as a percentage without being told; ``%`` is
#: doing its work at the readings that could be mistaken for something else.
_BATTERY_FULL = "100"


def _braille_fill(steps: int) -> str:
    """A single braille cell filled from the bottom by ``steps`` (0–8) half-rows.

    Each dot row is two steps: the left dot lights first, then the right one completes the
    row, so the fill climbs in half-rows rather than whole ones and one cell carries eight
    distinguishable levels.
    """
    steps = max(0, min(_BATTERY_STEPS, steps))
    bits = 0
    for step in range(steps):
        row, col = 3 - step // 2, step % 2
        bits |= _BRAILLE_DOTS[col][row]
    return chr(_BRAILLE_BASE + bits)


def _battery_steps(percent: int) -> int:
    """How many half-rows a charge lights: one rung per 12.5%, never fewer than one.

    The bands are ``0–12.5`` → one rung … ``87.5–100`` → the full cell, each one taking its
    lower bound. A pack on its last percent still lights a single dot, so the gauge is never
    an empty cell while the pack is still running.
    """
    pct = max(0, min(100, int(percent)))
    return max(1, min(_BATTERY_STEPS, int(pct * _BATTERY_STEPS / 100) + 1))


def _battery_band(percent: int) -> str:
    """The band a charge draws in (see :data:`_BATTERY_BANDS`) — always the *true* charge.

    Read off the pack rather than off the cell, so the charging sweep keeps saying what the
    pack holds while its fill climbs through frames that mean nothing on their own.
    """
    for floor, style in _BATTERY_BANDS:
        if percent >= floor:
            return style
    return "batt.low"


def battery_cell(percent: int, *, charging: bool = False, frame: int = 0) -> Text:
    """The status-bar battery gauge: one filled braille block, then its ``%``.

    The cell is a lit foreground over its own darker ground — a *block*, coloured by band
    (:data:`_BATTERY_BANDS`): light green on green over half, yellow on brown over a quarter,
    light red on red below. Inside that block the braille fills from the bottom up in eight
    half-row steps (see :func:`_braille_fill`), one rung per 12.5% of charge, the left dot of
    a row lighting before the right. So the block answers *roughly how much* from across the
    room and the dots answer *exactly how much* on a second look. Two live states animate off
    the caller's ``frame`` counter (advanced one step per repaint tick), so the gauge moves
    without any per-frame plumbing:

    * **Charging** overrides the fill: the cell sweeps empty-to-full on a loop, so a
      plugged-in pack visibly climbs. The band does *not* sweep with it — colour keeps
      answering for the real charge, since a frame of the animation means nothing on its own
      — and neither does the number beside it, which stays the true percent throughout. The
      sweep climbs by whole dot rows, not by the fill's half-rows: it is a "power is coming
      in" animation rather than a reading, and its tick is slow enough (a step per repaint)
      that five frames say that better than nine.
    * **Below :data:`_BATTERY_FLASH_PCT`** — half the last dot's worth of charge, and only
      when nothing is charging it — the cell alternates between black dots on red and the
      light red on the bare page. Dropping the ground for a beat is a bigger change than any
      hue swap in a palette where every other cell is filled, which is the point: it is the
      last warning a handheld gives before it dies.

    Both run on every platform, stepping at whatever that platform repaints at — 2 s on the
    PicoCalc, where the header is rebuilt on the idle tick anyway, so an animation costs a
    colour swap in a frame already being painted and nothing else. Neither is behind
    ``Platform.effects``: one *is* the charging state, and the other is the last warning a
    handheld gives before it dies.

    A pack at 100% is drawn as **not charging** whichever way the flag reads: a full cell
    resting full is the honest picture, and a topped-off charger left plugged in shouldn't
    leave the gauge sweeping forever. It also drops its ``%`` (:data:`_BATTERY_FULL`), so
    that full reading is the same three cells as the 99% it keeps flipping back to and the
    header stops resizing under a pack on a charger.

    Args:
        percent: State of charge, 0–100 (clamped).
        charging: Whether the pack is taking charge (drives the fill sweep). Ignored at 100%.
        frame: A monotonically advancing tick; only its phase is read, so any
            steadily-incrementing integer animates the two live states.

    Returns:
        A Rich :class:`Text`: the coloured block, a space, and ``NN%`` in muted text — a
        full pack's bare ``100`` the one exception (:data:`_BATTERY_FULL`) — with the
        block's colours on the glyph's span alone, so the ground stops at the cell.
    """
    pct = max(0, min(100, int(percent)))
    # The sweep climbs by whole rows, so the loop stays legible. Colour comes off the pack
    # either way: a sweep frame is an animation, not a reading, and must not be coloured
    # as though it were one.
    sweeping = charging and pct < 100
    steps = (frame % _CHARGE_FRAMES) * 2 if sweeping else _battery_steps(pct)
    color = _battery_band(pct)
    if not sweeping and pct < _BATTERY_FLASH_PCT:
        color = "batt.flash.off" if frame % 2 else "batt.flash"
    # The colours ride the *glyph's own span*, never the Text's base style: the block paints
    # a background, and a base style merges into every span, so a base block would drag the
    # muted percent onto the coloured ground alongside the cell.
    out = Text()
    out.append(_braille_fill(steps), style=color)
    out.append(f" {_BATTERY_FULL if pct == 100 else f'{pct}%'}", style="muted")
    return out
