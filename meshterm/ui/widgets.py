# SPDX-License-Identifier: Apache-2.0
"""Rich widgets for many screens: banner, status pill, trace tables, and progress bars."""

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
    """The one-character mark for the openness of a channel. The screens for channels share it.

    ``＃`` marks a channel with a name-derived key (a ``#`` channel). ``🌐`` marks a
    well-known public channel with a fixed key (for example the firmware default
    ``Public``). ``🔒`` marks a private channel. The function sends each mark through
    :func:`~meshterm.ui.theme.glyph`, so the PicoCalc draws the compact ``# @ ⚿``
    instead. On one platform, all the glyphs have the same width (double on regular,
    single on the PicoCalc). Thus callers can put ``"{glyph} "`` before rows and the
    columns stay aligned. When the secret is not known, the function uses the name alone
    to guess.

    Args:
        name: The channel name.
        secret: The 16-byte secret of the channel, or ``None`` when only the name is known.

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


#: The concept icon for a region (the scope of a flood). Refer to the icon table of ``CLAUDE.md``.
REGION_ICON = "🔖"


def scope_text(scope: Scope | None, *, bare: bool = False) -> Text:
    """The only rendering of the scope of a flood, for each surface that states one.

    There are three forms, one for each :attr:`~meshterm.core.regions.Scope.state`:

    * ``scope yul`` is a scoped flood with a known region. The name has the ``scope``
      style (a region is not a node, so it never has a node hue).
    * ``unknown scope 3fa1`` (``? 3fa1`` bare) is a scoped flood that no known name
      reproduces. The widget shows the code, so that the user can still see that two
      packets share a region. It is ``muted`` because it names nothing.
    * ``unscoped`` is a plain flood. It is ``muted`` because it is the ordinary case, and
      the widget states it without stress.

    ``None`` (a direct packet, or a packet whose route type MeshTerm did not keep) draws
    nothing. A repeater never filters these by region, so a word there claims a meaning
    that it does not have.

    Args:
        scope: The scope of the packet
            (:meth:`~meshterm.core.region_store.RegionStore.scope_of`).
        bare: Remove the ``scope`` lead-in and draw the region name alone, for a lane
            whose heading already says what it holds.

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
        # Do not put a `·` inside the text. A title chains its status atoms with `·`, and
        # ``scoped · 3fa1`` there looks like two atoms, but it is one fact.
        text.append("?" if bare else "unknown scope", style="muted")
        if scope.code:
            text.append(f" {scope.code}", style="muted")
    else:
        text.append("unscoped", style="muted")
    return text


def identity_label(label: str | None) -> str | None:
    """The default node resolver: it does not change labels."""
    return label


def banner(
    profile: str | None, mock: bool, selected_device: DiscoveredDevice | None = None
) -> Panel:
    """Build the header panel of the application.

    Args:
        profile: The name of the active device profile, if any.
        mock: ``True`` if the simulator is in use.
        selected_device: The discovered device that the user chose for this session, if
            any. The panel shows it when no named profile is in use.

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
    """Create a progress bar with the theme, for trace sweeps.

    Args:
        console: The console to render into.

    Returns:
        A configured :class:`rich.progress.Progress` (use it as a context manager).
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
    """Render an ``origin -> destination`` link through the only path widget.

    It is a :func:`path_text` of two nodes in the trace form. A known node is
    ``name (hash)``, an unknown node is its bare hash, and our device is its label (plus
    ``device_hash`` when the caller supplies it). The function truncates hashes to
    ``hash_bytes``, so that they match the way the trace command addressed each node.

    Args:
        origin: The transmitting node (``None`` = our device).
        destination: The receiving node (``None`` = our device).
        device_label: The label for our device.
        resolve: Maps a raw hop hash to a friendly name when the node is known.
        hash_bytes: The path-hash width (bytes) to which the function truncates the
            hashes that it shows.
        device_hash: The key or hash of our device. It is shown with its label when it
            is known.

    Returns:
        A :class:`Text` such as ``us (a1) → Alice (3d)`` with a muted arrow.
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
    """Render the SNR of each hop of all traces side by side, with one column for each trace.

    The table shows the hops as ``origin -> destination`` links. The first link starts at
    our device, and the last link returns to it. The table aligns the hops by position
    across the traces. Thus each column is the SNR readings of one trace down the shared
    path. Each node has its hash at the path-hash width of the command. A ``min`` row at
    the end shows the bottleneck of each trace. These are the values for each trace that
    the *median min SNR* of the run summarizes. Traces that never received a reply
    appear as a ``✗`` column.

    Args:
        traces: The individual traces to show, in the order of the run.
        device_label: The name to show for our device at the endpoints of the path.
        resolve: Maps a raw hop hash to a friendly contact name when it is known.
        device_hash: The key or hash of our device. The table adds it to the endpoints of
            the path.

    Returns:
        A Rich :class:`Table` with one column for each trace.
    """
    target = traces[0].target if traces else ""
    # All traces in a run have the path-hash width of the command. Take it from the first
    # successful trace, so that node hashes render at the width at which they were
    # addressed.
    hash_bytes = next((t.path_hash_bytes for t in traces if t.success), None)
    table = Table(title=f"Trace → {target}", border_style="muted", expand=False)
    table.add_column("HOP", justify="right", style="muted")
    table.add_column("FROM → TO")
    for i in range(1, len(traces) + 1):
        table.add_column(f"#{i}", justify="right")

    # Index the edges of each trace by hop position. Thus the columns stay aligned also
    # when the traces take paths of different lengths (a missing hop shows as a muted
    # dash).
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

    # The bottleneck of each trace: the weakest link, from which MeshTerm takes the median
    # min SNR of the run.
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
    """Render a sequence of hops compactly on one line. This is the only path widget.

    MeshTerm shows a walked, relayed, or planned sequence of hops in this one way, in each
    place where it appears: the ``via`` row of a packet, the delivery paths of a message,
    a feed note, and the walked route or planned spec of a trace. Each hop renders as its
    resolved name, in the hue of the app for that name. Our node is pure white. An
    unnamed hop renders as its hash in the grey of ``node.unknown``. (Colour marks an
    identified node, as in the path graph. A hop that MeshTerm cannot resolve never has
    the hue that its bytes give.) Muted ``→`` arrows join the hops. By default, the
    widget does not repeat a hash after a name, so the compact form fits in a row of 72
    columns.

    These options are for traces. ``show_hash`` adds to each named hop the hash by which
    it is addressed (``Alice (3d63)``). A ``None`` hop is our device at the endpoints of a
    route. ``dim_from`` fades the tail that a caller wants the user to read as automatic
    (the mirrored return leg of a boomerang).

    ``hash_as_name`` shows an *unnamed* hop as its own identity. The identity is the hash
    at the width of the path-hash mode (``prefix_bytes``), in muted grey with no prefix
    lit. (Colour is the signal "this is a name", and there is no name.) The widget adds
    to it the byte by which it is addressed, as for a named hop (``e839f2 (e8)``). By
    default, the widget instead lights the compact addressed hash. An empty path reads as
    ``empty``.

    Args:
        hops: The hops in the order of propagation. They are hex hashes, and ``None``
            marks our device. (The function skips empty strings. ``dim_from`` counts the
            hops that are rendered.)
        resolve: Maps a hop hash to a friendly name when it is known.
        prefix_bytes: The path-hash width at which the identity hash of an unnamed hop
            shows under ``hash_as_name``. (In all other cases, unnamed hops are fully
            grey, and no prefix is lit.)
        self_name: The name of our node. The function draws a resolved name that matches
            it in the white ``you`` style. It is also the name of each ``None`` device
            hop.
        empty: The muted text to show when there are no hops (for example ``"direct"``).
        show_hash: Add the hash in parentheses to the named hops (and to our device, with
            ``device_hash``). This is the form for traces.
        hash_bytes: Truncate the hashes that the function shows or adds to this byte
            width (the width at which the hops were addressed). ``None`` shows them whole.
        device_hash: The key of our device. The function adds it to the ``None`` hops when
            ``show_hash`` is on.
        dim_from: Render the hops at this index and after it, and the arrows into them,
            as faint (the return leg of a planned route). ``None`` dims nothing.
        hash_as_name: Show each unnamed hop as its identity hash, in muted grey at the
            ``prefix_bytes`` width, with the byte by which it is addressed
            (``hash_bytes``), as for a named hop. The default is the compact addressed
            hash with a lit prefix.

    Returns:
        A one-line :class:`Text`. When the space is narrower than the path, the caller
        decides what to do for each surface: ellipsize (``no_wrap``), wrap under a
        hanging indent, or crop with horizontal scrolling.
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
    """The warning for a path that touches one hop twice, or ``None`` when there is none.

    A route graph that is drawn with ``allow_duplicate_nodes``
    (:func:`~meshterm.ui.pathgraph.render_path_graph`) puts two markers with the same name.
    The ``via`` row above it names the same node twice. Both are correct, but both confuse
    the user if nothing explains them. This function makes the one line that explains
    them. It does not choose between the two readings, because nothing in the packet can.
    An observed chain addresses its hops with a hash that is one byte wide. Thus a repeat
    is as probably two different nodes that have the same byte as one node that the packet
    really passed twice. The function names the repeated hops through the only path widget
    (:func:`path_text`). Thus each hop has its own name hue, the same as in the ``via``
    row with which the user compares it.

    The function takes the repeated hops and not the path itself. Thus the caller decides
    what "repeated" means for its surface. One path asks
    :func:`~meshterm.ui.pathgraph.revisited_hops`. A surface that draws a *fan* of paths
    must join the own revisits of each path. A hop that two different routes share is not
    a revisit. If the surface pools their hops, it wrongly calls that hop a revisit.

    Args:
        repeats: The hops to warn about, which are already known to be repeated (empty =
            no warning).
        resolve: Maps a hop hash to a friendly name when it is known.
        prefix_bytes: The path-hash width to light in the hash of an unnamed hop (0 =
            none).
        self_name: The name of our node, so that a repeat of *us* draws in the white
            ``you`` style.

    Returns:
        A one-line :class:`Text` that fits in 72 cells for the paths that a radio really
        carries. ``None`` when ``repeats`` is empty and there is nothing to warn about.
    """
    if not repeats:
        return None
    # The mark has its own span and does not use the base style of the Text. Thus nothing
    # that follows can inherit ``warn``. The names keep their node hues, and the prose
    # stays muted.
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
    """One node of :func:`path_text` (refer to that function for the rendering rules)."""
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
        # There is no name, so the hash is the identity of the node. Show it at the width
        # of the path-hash mode, fully muted (prefix_bytes=0 lights nothing, because colour
        # is the signal "this is a name", and there is no name). Then add the byte by which
        # it was addressed, as for a named hop, unless that byte is already the whole hash
        # that the function shows. Thus a mode of 3 bytes reads ``e839f2 (e8)``, and it
        # still cross-references a route graph that has labels of one byte.
        identity = _shorten_hash(hop, prefix_bytes or None)
        text = highlighted_hash(identity, 0, known=False)
        if show_hash and shown and shown != identity:
            text.append(f" ({shown})", style=note_style)
        return text
    # No name resolved: the node is unknown, and the hash of an unknown node is fully grey.
    # The prefix is never lit (colour marks an identified node, refer to highlighted_hash).
    return highlighted_hash(shown, prefix_bytes, known=False)


def highlighted_hash(
    value: str, prefix_bytes: int, width: int | None = None, *, known: bool = True
) -> Text:
    """Render a hex key with its leading path-hash prefix lit. This is the only hash widget.

    MeshTerm displays a hash in this one way, in each place where one appears. The first
    ``prefix_bytes`` bytes are the slice that other nodes address in a forced trace path
    (the path hash). The widget lights them in the palette hue that comes from the hash of
    the node, which is the same hue as its name (refer to
    :func:`~meshterm.ui.theme.node_style`). The remainder is muted. Thus the addressable
    prefix is clear in the otherwise full key. If the ``width`` budget is shorter than the
    key, the widget ellipsizes the key. The ``…`` has the colour of the digit that it
    replaces, so a highlight that is wider than the budget still reads as one.

    Colour marks an *identified* node (JP, 2026-08-08). A hash for which nobody can name
    the node has ``known=False``, and it reads fully in the ``node.unknown`` grey of the
    app. An unknown node draws in the same way in the path graph. It never has a hue that
    its bytes give. In the same way, ``prefix_bytes=0`` on a known node lights
    nothing, and a key that is the name takes ``node.unknown`` and not ``muted``. It is the
    content of the row. It is not the dim tail of a hash whose head already has the hue.

    Args:
        value: The key as hex. It can have the ``0x`` prefix and mixed case.
        prefix_bytes: The number of leading bytes that the current path-hash mode
            addresses. ``0`` (or a negative value) leaves the whole key without a
            highlight.
        width: The display budget in cells. The widget truncates a longer key to the
            whole leading bytes that fit in ``width - 1`` cells (an even number of digits,
            because a hash reads in bytes of two hex digits each) plus an ellipsis. It
            pads a shorter key on the right to the budget, so that the lanes stay
            aligned. ``None`` shows the whole key, without padding.
        known: ``True`` if the hash belongs to a node that the caller can identify (name
            it, or place it as a real contact). ``False`` makes the whole run grey,
            also the prefix.

    Returns:
        A styled :class:`Text` of the key (exactly ``width`` cells when it is given).
    """
    raw = value.lower().removeprefix("0x")
    split = max(0, prefix_bytes) * 2 if known else 0
    pad = 0
    ellipsis = False
    if width is not None and len(raw) > width:
        # Truncate on a byte boundary. Keep an even number of hex digits. The ellipsis uses
        # the next cell, and any odd cell that is left is padding. Thus a digit from the
        # middle of a byte never shows, and the lane still has exactly ``width`` cells.
        kept = max(0, width - 1)
        kept -= kept % 2
        raw, ellipsis = raw[:kept], True
        pad = width - kept - 1
    elif width is not None:
        pad = width - len(raw)
    # The hue comes from the key that is not truncated (any prefix gives the same hue). But
    # only an identified node has a hue. The hash of an unknown node is grey everywhere.
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
    """A node name drawn as the single chip of a path line: the only frame for a name.

    This is not an imitation of the chip language. It uses the chip language. The function
    builds a :class:`~meshterm.ui.pathline.PathLine` with one hop and asks it for its line.
    Thus the fill, the ink, the padding, the rounded cap, and the closing point are the
    same as those of the segments of a route today, and they can never be different from
    them (JP, 2026-08-12). The colour reads the same as in a route. The name has the hue
    of the node, on a darker fill of that hue. A node that no key can place has the
    keyless grey. Our own end is the ``★`` of the map on its neutral dark grey, and not a
    name.

    The degradation is also inherited. Where the terminal cannot draw powerline
    separators, a path line is text in arrow mode, and a single hop is only the name in its
    own hue. This is what a sender label was before it had a frame.

    Args:
        label: The name as it must read (it is already resolved, and this function never
            renames it). The function ignores it when ``you`` is true, because our own end
            is the star and never our name.
        key: Any known prefix of the key of the node, which selects the hue. ``None`` is a
            node that MeshTerm cannot place. It has the keyless grey, because colour is
            only for identities that have a key.
        you: ``True`` if this is our node. It is the ``★``, the same as a route draws our
            end of it.

    Returns:
        The chip as a one-line :class:`Text`.
    """
    hop = PathHop(SELF_GLYPH, you=True) if you else PathHop(label, key=key or None)
    return PathLine([hop]).text()


def _shorten_hash(value: str, hash_bytes: int | None) -> str:
    """Return ``value`` as bare hex truncated to ``hash_bytes`` bytes.

    Args:
        value: A hex hash. It can have the ``0x`` prefix and mixed case.
        hash_bytes: The width in bytes to which the function truncates. When it is empty
            or unknown, the function returns the full value.

    Returns:
        Hex in lower case, without the ``0x`` prefix, with a maximum width of
        ``hash_bytes`` bytes.
    """
    raw = value.lower().removeprefix("0x")
    return raw[: hash_bytes * 2] if hash_bytes else raw


#: The pure-white hue of our node, which matches the ``you`` style. An endpoint that is us
#: (and each relay that resolves to our name) takes it instead of its palette colour.
_SELF_RGB: RGB = (255, 255, 255)


def _name_rgb(name: str, key: str | None = None) -> RGB:
    """The RGB of the stable palette hue of a node (``theme.name_style`` without its bold).

    A name that has no key that MeshTerm can resolve has the style ``node.unknown`` (a
    theme name, not a hex). Thus it gets that grey here. This is the raster twin of the
    rule of the app that a name without a key stays grey.
    """
    hexpart = name_style(name, key).split()[-1]
    return parse_hex(hexpart) if hexpart.startswith("#") else mark_rgb(hexpart)


def name_rgb(name: str, key: str | None = None) -> RGB:
    """The truecolour of the stable palette hue of a node, for a braille canvas.

    This is the public face of :func:`_name_rgb`. It gives the same hue that
    :func:`~meshterm.ui.theme.name_style` paints a name in (it comes from the hash when
    ``key`` is given), as an ``(r, g, b)`` tuple that a raster can plot. A caller that
    colours node markers by their mesh identity (the drawing of the area in the trophy
    case) makes them through this function.
    """
    return _name_rgb(name, key)


#: Maps a relay hash to its node type (refer to the ``NODE_TYPE_*`` constants), or to
#: ``None`` when the type is unknown. It is the hook that lets the route graph mark a
#: repeater as ``▲`` and not only as a generic ``●``. MeshTerm never passes the endpoint
#: sentinels to it.
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
    """Build the callbacks for each node that draw a route on the only route graph (``pathgraph``).

    The dialog for message paths and the Trophy case both draw their graphs with this
    shared presentation. The two endpoints have node names. Each relay between them has
    its marker and the first byte of its hash. Each label has the name hue of its node
    (us: the pure-white ``you``), so a byte reads as the mesh name that it stands for. The
    function maps the two endpoint sentinels of the graph and each relay hash to a glyph,
    a label, and a label colour. The sentinels are :data:`~meshterm.ui.pathgraph.SRC_NODE`
    on the left and :data:`~meshterm.ui.pathgraph.DST_NODE` on the right.

    The right endpoint is *usually* us, and it was once always us. That is correct for a
    route that we walked or a message that we received. It is wrong for a message that we
    sent. With our name on both ends, the graph of each outgoing message drew a round trip
    (JP, 2026-09-02). ``destination`` names the endpoint when it is another node.

    Args:
        resolve: Maps the hash of a relay to a friendly name when one is known.
        self_name: The name of our node. It is the label of the right endpoint, drawn in
            white.
        source: The display name of the left endpoint (the origin of a message, or us for
            a walk that starts at home. Pass ``self_name`` to draw both ends as us).
            ``None`` reads as an unknown origin ``?``.
        destination: The display name of the right endpoint, when the route does not end at
            us (the recipient of a message that we sent). ``None`` (the default) draws us.
            A walk and each received message need this.
        type_of: Maps the hash of a relay to its node type, so that a relay draws its own
            map marker (``▲`` repeater, ``■`` room, ``◉`` sensor) in the shared palette
            instead of a generic dot. For ``None``, or a hash whose type it cannot
            resolve, the old fallback stays: a named dot or an unknown ring.
        key_of: Maps the display name of ``source`` back to the key of its node (refer to
            :func:`~meshterm.services.trace_runner.make_name_key_resolver`), so that the
            label of the left endpoint has its hue from the key. For ``None``, or a name
            that it cannot place, the origin stays muted, because colour is only for
            identities that have a key.

    Returns:
        The ``(glyph_of, label_of, label_rgb_of)`` triple to give to
        :func:`~meshterm.ui.pathgraph.render_path_graph`.
    """
    src_is_self = bool(source) and source == self_name
    dst_is_self = not destination or destination == self_name

    def glyph_of(node: str) -> tuple[str, str]:
        """Us: a star. A relay with a type: its map marker. A named node: a dot. Else: a ring."""
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
        """The endpoints have their names, and the relays have their first hash byte."""
        if node == DST_NODE:
            return (self_name or "you") if dst_is_self else destination
        if node == SRC_NODE:
            return source or "?"
        return node[:2]

    def label_rgb_of(node: str) -> RGB:
        """The colour of a label: the name hue of its node, white for us, the ring if unknown.

        A relay that MeshTerm cannot name keeps the colour of its *marker*. Thus the hash
        under the ``▲`` of a repeater reads as that repeater, and not as a stray grey
        byte. For this reason, this function resolves what ``glyph_of`` returns, a style
        name or a hex.
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
    """The route that a trace walked, as the line object of the only path widget.

    It is the same route that :func:`_route_text` renders. This function returns the
    :class:`~meshterm.ui.pathline.PathLine` itself. Thus a surface that has space can wrap
    it at the hop boundaries (:meth:`~meshterm.ui.pathline.PathLine.wrapped`) and does
    not take the one-line text and fold it in the middle of a name.

    Args:
        result: The trace for which to build the route.
        device_label: The name to show for our device at the endpoints of the path.
        resolve: Maps a raw hop hash to a friendly contact name when it is known.
        device_hash: The key or hash of our device. The function adds it to the endpoints
            when it is known.
        bare_self: Draw both ``us`` endpoints as their bare arrow. A surface (the route
            lane of the trace screens) uses this where a walk that starts and ends on us
            is the premise and not news.
        show_hash: Add to each *named* hop the hash by which it was addressed. This is on
            for the table output of scripts, where the hash is half of the answer. It is
            off for the live screens. Their route lane reads as the sequence of nodes, and
            the lane for the wire spec below it has the hex. Unnamed hops always show their
            hash, because it is the only identity that they have.

    Returns:
        The :class:`~meshterm.ui.pathline.PathLine` of the route. When the trace recorded
        no hops, it has no hops and it reads ``no hops recorded``.
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
    """Render the route that a trace walked through the only path widget, on one line.

    The function shows the path that the trace really walked. This is the forced path, or
    the route that the device resolved with auto-routing. It uses the trace form. Each node
    has its hash at the path-hash width of the command, and our device is at both ends. An
    example is ``Me (a1b2) → Alice (3d63) → Bob (f2a1) → Me (a1b2)``.

    Args:
        result: The trace for which to show the route.
        device_label: The name to show for our device at the endpoints of the path.
        resolve: Maps a raw hop hash to a friendly contact name when it is known.
        device_hash: The key or hash of our device. The function adds it to the endpoints
            when it is known.

    Returns:
        A :class:`Text` with the sequence of nodes, or a muted note when there are no hops.
    """
    return route_path(result, device_label, resolve, device_hash).text()


def _hop_medians_table(
    hop_snrs: list[HopAggregate],
    device_label: str,
    resolve: NodeResolver = identity_label,
    hash_bytes: int | None = None,
    device_hash: str | None = None,
) -> Table:
    """Render the median SNR of each hop, aggregated across the traces of a run.

    Args:
        hop_snrs: The aggregates for each hop to show.
        device_label: The name to show for our device at the endpoints of the path.
        resolve: Maps a raw hop hash to a friendly contact name when it is known.
        hash_bytes: The path-hash width (bytes) to which the function truncates the node
            hashes that it shows.
        device_hash: The key or hash of our device. The function adds it to the endpoints
            of the path.

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
    """Summarize the aggregated trace statistics in a panel.

    The panel has the median SNR for each hop along the path (and not only the
    bottleneck). Thus a weak link anywhere in the route is visible. When the caller gives
    a representative ``route`` trace, the panel shows the path that it really walked as a
    sequence of nodes, for example ``Me → Alice → Bob → Me``.

    Args:
        stats: The aggregated statistics to show.
        device_label: The name to show for our device at the endpoints of the path.
        resolve: Maps a raw hop hash to a friendly contact name when it is known.
        route: A representative trace for which to show the route that it walked, if any.
        device_hash: The key or hash of our device. The function adds it to the endpoints
            of the route.

    Returns:
        A Rich :class:`Panel` with the route, the success rate, the robust SNR and RTT,
        and the medians for each hop.
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
        sections.append(Text())  # a blank line before the block of statistics
    sections.append(summary)
    if stats.hop_snrs:
        sections.append(Text("\nPer-hop medians", style="accent"))
        hash_bytes = route.path_hash_bytes if route is not None else None
        sections.append(
            _hop_medians_table(stats.hop_snrs, device_label, resolve, hash_bytes, device_hash)
        )
    body: Text | Group = sections[0] if len(sections) == 1 else Group(*sections)
    return Panel(body, title="[accent]Trace summary[/accent]", border_style="accent", expand=False)


# The glyphs and colours of the node types. They are the same as the marker palette of the
# map in the whole app (refer to ui.map_render). Our node is the yellow ``★``, plain nodes
# are the loud pink ``●``, and repeaters are the calmer violet ``▲``. The other types have
# hues that are safe on the map and different from those: a white square for rooms (the
# house glyph was difficult to read) and an orange dot with a ring for sensors. All are
# single-width BMP glyphs, so the columns stay aligned. The colours are the ``type.*``
# entries of the theme and not raw hex. Thus the 16-slot console chooses its slot on
# purpose (otherwise the violet downsamples to grey, and a repeater looks like an unknown
# node). On the regular platform, they *are* the hues of the map.
NODE_GLYPHS: dict[int, tuple[str, str]] = {
    NODE_TYPE_REPEATER: (REPEATER_MARK[0], "type.repeater"),
    NODE_TYPE_ROOM: ("■", "type.room"),
    NODE_TYPE_SENSOR: ("◉", "type.sensor"),
    NODE_TYPE_CHAT: (NODE_MARK[0], "type.node"),
}
DEFAULT_GLYPH: tuple[str, str] = (NODE_MARK[0], "type.node")


def node_marker(node_type: int | None) -> tuple[str, RGB]:
    """The glyph and colour from the map palette for a node type, as a ``(glyph, rgb)`` pair.

    These are the shared node-type marks (``▲`` repeater, ``■`` room, ``◉`` sensor, ``●``
    plain node) in the own colours of the map. This function makes them for a braille
    raster, as :data:`NODE_GLYPHS` makes them for a Rich row. Thus a spatial drawing pins
    its nodes with the same glyphs and hues that the map and the list of nodes use. Both
    read the same ``type.*`` entry of the theme (here through
    :func:`~meshterm.ui.theme.mark_rgb`). Thus the raster and the row agree on the colour
    that the palette of the platform can show. An unknown type uses the plain node mark.
    """
    glyph, style = NODE_GLYPHS.get(node_type or -1, DEFAULT_GLYPH)
    return glyph, mark_rgb(style)


def self_marker() -> tuple[str, RGB]:
    """The map marker of our node, the yellow ``★``, as a ``(glyph, rgb)`` pair."""
    return SELF_MARK[0], parse_hex(SELF_MARK[1])


# A heat-map gradient for the heard age of a node, from the hottest (heard most recently) to
# the coldest: white → yellow → orange → red → grey. Each stop pairs an age anchor (log10 of
# the seconds since the node was heard) with an RGB colour. :func:`_recency_style`
# interpolates continuously between the stops. Thus the colour glides with the recency, and
# does not snap between a few separate shades.
_HEAT_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (math.log10(300), (255, 255, 255)),  # ≤5m: white (fresh)
    (math.log10(3600), (250, 204, 21)),  # ~1h: yellow
    (math.log10(21600), (251, 146, 60)),  # ~6h: orange
    (math.log10(86400), (248, 113, 113)),  # ~1d: red
    (math.log10(604800), (148, 163, 184)),  # ~1w: grey
    (math.log10(2592000), (100, 116, 139)),  # ~30d+: cold slate
)
_RECENCY_NEVER = "#64748b"  # never heard: the coldest slate


def age_seconds(when: datetime | None) -> float | None:
    """The seconds since ``when`` (aware UTC), or ``None`` when it is unknown or naive."""
    if when is None or getattr(when, "tzinfo", None) is None:
        return None
    return max(0.0, (utcnow() - when).total_seconds())


def format_age(secs: float | None) -> str:
    """A compact relative age (``now``, ``5m``, ``3h``, ``2d``, ``4w``), or ``never``."""
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
    """A section heading in a screen body: an accent title, and an optional muted ``  ·  note``.

    This is the only form for a heading that is *in* the prose or the drawings of a page
    (Volume, SNR, and Rhythm in the Time Machine, and Stats, Area walked, and Route in a
    record). It is not the ``── Label ──`` landmark of a grouped list
    (:func:`~meshterm.ui.menus.section_heading`), which pins and which the section jumps
    go to. The note explains the block: its unit, its window, and what its labels mean. It
    is muted so that the title stays the landmark.

    Args:
        title: The name of the section, in sentence case.
        note: An aside after the wide separator, or ``""`` for none.

    Returns:
        The heading as one styled line.
    """
    text = Text(title, style="accent")
    if note:
        text.append(f"  ·  {note}", style="muted")
    return text


def format_ago(secs: float | None) -> str:
    """The relative-age *phrase* (``now``, ``5m ago``, ``never``) for prose.

    This is the standard grammar for each row "heard … (…)" and "delivered …". A packet
    that MeshTerm heard now reads as the bare ``now``, and an unknown age reads as the
    bare ``never``. Neither takes the suffix, because "now ago" is nonsense. Each measured
    age reads ``5m ago``. Callers that put an age in a sentence or in parentheses use this
    function. :func:`format_age` stays the bare form for the columns of aligned age lanes.
    """
    age = format_age(secs)
    return age if age in ("now", "never") else f"{age} ago"


def _recency_style(secs: float | None) -> str:
    """The heat-map colour for a heard age of ``secs`` for a node (hotter = more recent).

    The function calls the implementation that is bound to the platform. It is the
    continuous gradient of the regular platform, or the quantized steps of the PicoCalc (a
    16-slot palette has no space for a glide).
    """
    return _recency_impl(secs)


def _recency_gradient(secs: float | None) -> str:
    """The heat of the regular platform: continuous interpolation over :data:`_HEAT_STOPS`.

    The function interpolates the RGB channels between the two stops on each side of
    ``secs`` (in log-age space). Below the first stop, it gives white. Above the last stop,
    it gives cold slate.
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


#: The heat ladder as ``(younger than, style)``, with the hottest first (JP's spec). Each
#: boundary is a plain human unit. Thus a colour change is always at a number that a person
#: can say aloud ("under five minutes", "over a week"). After one year, a node is not
#: different enough from a node that was never heard, so the two share the coldest step.
_HEAT_STEPS: tuple[tuple[float, str], ...] = (
    (300, "heat.now"),  # under 5 minutes: white
    (3600, "heat.minutes"),  # 5 minutes      : yellow
    (86400, "heat.hours"),  # 1 hour         : light red
    (604800, "heat.days"),  # 1 day          : brown
    (2592000, "heat.weeks"),  # 1 week         : red
    (31536000, "heat.months"),  # 1 month        : light grey
)  # 1 year / never: dark grey (heat.never)


def _recency_quantized(secs: float | None) -> str:
    """The heat of the PicoCalc: the gradient in steps, on the ``heat.*`` rungs of the theme.

    The scale and the anchors are the same as for the gradient of the regular platform. A
    console that cannot use a hue for each second uses one hue for each unit instead (refer
    to :data:`_HEAT_STEPS`).
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
    """Pick the heat implementation for the platform (runs now and at each switch)."""
    global _recency_impl
    _recency_impl = _recency_gradient if platform.truecolor else _recency_quantized


def _key_id(value: str) -> str:
    """Normalize a key or prefix to the 12-hex id, in lower case, that matches heard nodes."""
    return value.lower().removeprefix("0x")[:12]


def contact_packets(contact: Contact, counts: dict[str, int]) -> int | None:
    """The count of overheard packets for ``contact``, or ``None`` if it was never overheard."""
    ident = contact.public_key or contact.key_prefix
    return counts.get(_key_id(ident)) if ident else None


# The columns by which the contact list can be sorted, from left to right, and the direction
# in which each one opens: name A→Z, most recently heard first, most packets first. We chose
# these so that a new sort shows the interesting end at the top.
_SORT_COLUMNS: tuple[str, ...] = ("name", "heard", "packets")
_SORT_OPENS_ASCENDING: dict[str, bool] = {"name": True, "heard": True, "packets": False}


@dataclass
class ContactsSort:
    """The column by which the contact list is sorted, and the direction.

    ``column`` is one of :attr:`columns`. ``ascending`` sorts the metric of the column from
    low to high. For the name this is A→Z. For the *age* it is the most recently heard
    first (ascending). For the packet count it is from low to high. The screen changes this
    object in place when the user presses the arrows.

    The sort *ring* belongs to each instance. By default, :attr:`columns` and
    :attr:`opens_ascending` are the three columns of the Contacts list
    (:data:`_SORT_COLUMNS`). But the picker of the Time Machine passes a wider set. It adds
    a sortable ``hash`` column. Thus the same model works for both, and the two screens do
    not need to share a global list of columns of the module.
    """

    column: str = "name"
    ascending: bool = True
    columns: tuple[str, ...] = _SORT_COLUMNS
    opens_ascending: dict[str, bool] = field(default_factory=lambda: _SORT_OPENS_ASCENDING)

    @classmethod
    def names(cls, columns: tuple[str, ...] = _SORT_COLUMNS) -> tuple[str, ...]:
        """The orders that :meth:`from_name` accepts: what a caller can validly ask for.

        :meth:`from_name` answers "which sort is this?" and accepts anything that it does
        not recognize. A surface that must *refuse* an unknown order needs the set itself,
        and this method returns it.
        """
        return columns

    @classmethod
    def from_name(
        cls,
        name: str,
        columns: tuple[str, ...] = _SORT_COLUMNS,
        opens_ascending: dict[str, bool] | None = None,
    ) -> ContactsSort:
        """Build a sort for ``name`` over ``columns``, in the natural direction of that column.

        If ``name`` is not one of ``columns``, the function uses the first column of the
        ring. Thus an old stored key cannot block the sort. If the caller does not give
        ``opens_ascending``, the function uses the directions of the Nodes list.
        """
        opens = opens_ascending if opens_ascending is not None else _SORT_OPENS_ASCENDING
        column = name if name in columns else columns[0]
        return cls(column, opens[column], columns, opens)

    def move(self, delta: int) -> None:
        """Move the active column ``delta`` places (it wraps), and use its natural direction."""
        index = (self.columns.index(self.column) + delta) % len(self.columns)
        self.column = self.columns[index]
        self.ascending = self.opens_ascending[self.column]


def ordered_contacts(
    contacts: list[Contact], counts: dict[str, int], sort: ContactsSort
) -> list[Contact]:
    """The contacts, sorted as ``sort`` says (our node is pinned separately, above these).

    The metric of the active column sets the order (reversed for a descending sort). A tie
    always breaks by the case-folded name, *ascending*. Thus two contacts with the same
    metric (for example the same ``heard`` age) keep a stable A→Z order, and the order does
    not flip with the primary direction. The rows that were never heard or never overheard
    have an extreme metric, so they gather at the ascending end.
    """
    if sort.column == "heard":
        # Use one clock snapshot for the whole sort. If each row calls utcnow(), two
        # identical last_seen stamps differ by a few microseconds, and the tie-break by
        # name does not work.
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

    # Sort by name ascending first, then make a stable sort by the primary metric. Rows with
    # the same metric keep their A→Z order in both directions. (If the code reverses the
    # whole list, it flips the tie-break.)
    ordered = sorted(contacts, key=lambda c: c.name.casefold())
    ordered.sort(key=metric, reverse=not sort.ascending)
    return ordered


def _sort_header(label: str, column: str, sort: ContactsSort) -> str:
    """A column header: plain and muted, or lit with a direction triangle when it is the sort key.

    The name of the active column and its triangle are lit together in the ``cursor`` white
    of the app, so that the selection with left and right is clear. The triangle is ``▲``
    for ascending and ``▼`` for descending. The ink is the same as that of the highlighted
    *row*, because it is the same claim on the other axis. The column keeps its width
    (refer to :func:`contacts_table`), so a change of the sort does not shift the row.
    """
    if column != sort.column:
        return label
    triangle = "▲" if sort.ascending else "▼"
    return f"[cursor]{label} {triangle}[/]"


#: The word of the legend for each node type: the one name of the type
#: (:data:`NODE_TYPE_LABELS`). The exception is where a shorter word is clear next to its
#: glyph: ``room`` for a room server (JP, 2026-10-02). In all other places, including the
#: CLI, the name stays whole.
_LEGEND_WORDS = {**NODE_TYPE_LABELS, NODE_TYPE_ROOM: "room"}


def node_type_legend(indent: str = "", width: int | None = None) -> Text:
    """The legend for the node-type marks: ``★ you   ▲ repeater   ● companion   …``.

    Each glyph has its shared map colour (refer to :data:`NODE_GLYPHS`), and its name
    follows it in muted style (:data:`_LEGEND_WORDS`). This is the only legend for each
    surface that draws node markers with types: the contacts list under its table, and the
    route graph under its lanes. Thus one glyph has one meaning in the whole app.

    The legend has one line where it fits: 52 cells, inside the 53 cells of the handhelds.
    Where it does not fit (an indent, a narrower box), it breaks onto a second line between
    entries and never inside an entry. Thus a mark is never separated from its name, and
    no name is cropped.

    Args:
        indent: The leading spaces that put the legend under the body of a table or graph.
        width: The cells that are available. ``None`` keeps the legend on one line.
    """
    entries = [Text.assemble((SELF_MARK[0], SELF_MARK[1]), (" you", "muted"))]
    for node_type in (NODE_TYPE_REPEATER, NODE_TYPE_CHAT, NODE_TYPE_ROOM, NODE_TYPE_SENSOR):
        glyph, color = NODE_GLYPHS[node_type]
        entries.append(Text.assemble((glyph, color), (f" {_LEGEND_WORDS[node_type]}", "muted")))

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


#: The blank rows of air around a tab strip: one above and one under on the desktop, and
#: none on the PicoCalc (JP, 2026-08-08). On the PicoCalc, rows are scarce, and both lines
#: go to the stage instead (the ceiling of the route graph, and the room for the preview of
#: the location to grow). The constant follows the same signal of frugality as the
#: borderless frame. MeshTerm binds it when the platform switches, as it does for each
#: constant that comes from the platform. It never branches for each paint.
_TAB_AIR = 1


@on_platform
def _bind_tab_air(platform: Platform) -> None:
    """Bind the air of the strip to the platform (runs now and at each switch)."""
    global _TAB_AIR
    _TAB_AIR = 1 if platform.frame_border else 0


def tab_air() -> int:
    """The blank rows that a tabbed page uses above and under its strip (refer to :data:`_TAB_AIR`).

    Each screen that draws :func:`tab_strip` reads it, for example the node page and a
    record. Thus the strip has the same air everywhere, and it loses the air on the same
    platform.
    """
    return _TAB_AIR


#: The viewport, in rows, under which a frame is *short* and uses its rows for content.
#: A tabbed page draws its strip on one row (refer to ``compact`` of :func:`tab_strip`).
#: The caption and the node-type legend of a graph step aside, so that the list under it
#: gets the rows. The boxed strip alone costs three rows. On the viewport of 11 rows of the
#: Cardputer, this was the difference between the actions of a page on the screen and
#: below the fold. The value comes from the viewport and not from the platform. Thus a
#: desktop terminal that the user drags to a short size gets the same result as the
#: handheld.
SHORT_FRAME_BELOW = 16


def short_frame(viewport: int) -> bool:
    """Check if a page with ``viewport`` rows is short (refer to :data:`SHORT_FRAME_BELOW`)."""
    return viewport < SHORT_FRAME_BELOW


def tab_strip(labels: Sequence[str], active: int, width: int, *, compact: bool = False) -> Group:
    """A boxed tab strip: each tab has a box on top, and the active tab is open into the page.

    This is the only navigation header for a screen that shows one view of the full height
    at a time, between a few named views (for example ``Info`` and ``Routes`` on the node
    detail page), and does not stack them. Each tab always has a box with a full top. The
    box has a dim ``faint`` outline by default, and a lit ``accent`` outline when the tab
    is active. It is not only the active tab that has a box. Thus the width of each tab is
    fixed, in any selection, and a switch of tabs never shifts the labels that follow it.
    Neighbouring tabs share a vertical line (one glyph, not two). Each shared *top* corner
    is drawn as if the tab on the left sits on top of the tab on the right. It "opens"
    (rounds toward the tab that starts there) only at the first tab and at the left edge of
    the active tab. It "closes" (rounds back, and gives the position to the tab on its
    left) in all other places. Thus the active tab is the one exception. It always wins both
    of its top corners, and it looks lifted in front of its neighbours.

    The *bottom* border is a single continuous rule with the accent colour. It is one line,
    so it has one colour throughout, the colour of the active tab. The inactive tabs sit
    flush against it. They have no corners or seams of their own there. The rule only
    passes behind them. It is broken only under the active tab, and thus the tab looks
    merged into the page below it. The rule turns up into ``╯`` (it closes the flat run
    from the west, and turns north into the wall of the tab). It leaves the width of the
    active tab open (no line, because that gap *is* the start of the page). Then it turns
    back down through ``╰`` (the wall meets the flat run that continues east) and goes on
    flat. A lone tab becomes the plain accent heading (the ``── Label ──`` form of
    :func:`~meshterm.ui.menus.section_heading`), because there is nothing to switch
    between or to layer. When ``width`` has more space than necessary, the tab boxes (the
    top row and the label row) are two columns from the left. The bottom rule is one
    continuous line in all cases, so it fills that margin and does not have the indent of
    the boxes. The screen that owns the strip changes the active index (``Tab`` and
    ``Shift+Tab``). The strip itself is only a presentation.

    Where rows are scarcer than that (``compact``, refer to :func:`short_frame`), the
    function draws the same strip on its own bottom rule, in one row:
    ``──┤ Info ├── Routes ──────``. The active tab stands on the rule between tees, and it is
    lit. An inactive tab is its label on the rule, in faint style. In both states, each tab
    has the same width, so a switch still never moves a label.

    Args:
        labels: The tab names, in the order of display.
        active: The index of the lit tab.
        width: The render width of the strip. The closing rule fills to it.
        compact: Draw the strip on one row instead of three.

    Returns:
        A :class:`~rich.console.Group` of one line (a lone tab, no tab, or a compact strip)
        or three lines (the shared top border, the boxed labels, and the shared bottom
        rule).
    """
    if not labels:
        return Group(Text(""))
    if len(labels) == 1:
        return Group(Text(f"── {labels[0]} ──", style="accent", no_wrap=True))
    if compact:
        row = Text("──", style="accent", no_wrap=True)
        for i, label in enumerate(labels):
            if i == active:
                row.append(f"┤ {label} ├", style="accent")
            else:
                row.append("─", style="accent")
                row.append(f" {label} ", style="faint")
                row.append("─", style="accent")
            row.append("─", style="accent")
        row.append("─" * max(0, width - len(row.plain)), style="accent")
        return Group(row)

    n = len(labels)
    inner = [f"  {label}  " for label in labels]
    content_width = sum(len(cell) for cell in inner) + (n + 1)
    margin = 2 if content_width + 2 <= width else 0
    pad = " " * margin

    def top_owner(junction: int) -> int:
        """The tab whose top corner glyph is at this junction (refer to the docstring rule)."""
        return junction if junction == 0 or junction == active else junction - 1

    def top_style(junction: int) -> str:
        return "accent" if top_owner(junction) == active else "faint"

    def bot_glyph(junction: int) -> str:
        """The glyph of the bottom rule at this junction.

        It is a corner that turns into or out of the wall of the active tab, or else a flat
        pass-through. It always has the accent colour. Refer to the enclosing docstring for
        the reason.
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
    """The node-type legend, with an indent so that it is under the body of the contacts table."""
    return node_type_legend(indent="  ")


def contacts_table(
    self_name: str,
    self_key: str,
    contacts: list[Contact],
    prefix_bytes: int,
    counts: dict[str, int],
    sort: ContactsSort | None = None,
) -> Group:
    """List our node and its known contacts with the age, packets, type, key, and legend.

    Our node is the first row (``★``, with the name in the white ``you`` style). The
    contacts follow in the order of ``sort``. A glyph for the type marks each node in the
    shared colours of the app. The name has the palette hue that comes from the hash of the
    node. The heard age glows with the heat of recency (brighter = fresher). The table
    shows the full key with its path-hash prefix lit. It cuts the key with an ellipsis only
    when the terminal is too narrow.

    Args:
        self_name: The advertised name of our node.
        self_key: The full public key of our node (hex). If it is blank, the table shows
            ``?``.
        contacts: The known contacts, listed after our node.
        prefix_bytes: The path-hash width in bytes to light in each key.
        counts: The counts of overheard packets, keyed by the 12-hex node id in lower case
            (from monitoring). A contact with no entry shows ``—``.
        sort: The active sort (column and direction). The default is name ascending. The
            header of the sorted column is lit cyan, with a triangle for the direction.

    Returns:
        A Rich :class:`Group` of the table without a frame and its legend of glyphs.
    """
    sort = sort if sort is not None else ContactsSort()

    # expand=True lets the key column (the only flexible column) take all the spare width.
    # It is also the only column that Rich squeezes when the terminal is narrow. The fixed
    # columns keep their natural size.
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
    # Each sortable header keeps two more columns for its " ▲" direction marker (with
    # min_width = label + 2). Thus the width is the same if the header is the active sort or
    # not. A change of the sort never makes a column wider and never shifts the rest of the
    # row.
    table.add_column("", no_wrap=True)  # node-type glyph
    table.add_column(_sort_header("NAME", "name", sort), no_wrap=True, min_width=6)
    table.add_column(
        _sort_header("HEARD", "heard", sort), justify="right", no_wrap=True, min_width=7
    )
    table.add_column(
        _sort_header("PKTS", "packets", sort), justify="right", no_wrap=True, min_width=6
    )
    # The full key. Rich cuts it to an ellipsis only when the row does not fit in another way.
    table.add_column("KEY", no_wrap=True, overflow="ellipsis", ratio=1, min_width=10)

    unknown = Text("?", style="muted")
    table.add_row(
        Text(SELF_MARK[0], style=SELF_MARK[1]),
        # Our name has the pure-white "you" style of the app, and never a palette hue.
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
    """Render each measured TX level of an optimization sweep, with the best row highlighted.

    Args:
        result: The optimization result to show.

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
        # Highlight each rate that is less than a perfect success rate. Reliability is first.
        rate = lv.success_rate
        rate_style = "ok" if rate >= 1.0 else ("warn" if rate > 0 else "err")
        rate_cell = Text(f"{rate:.0%}", style=rate_style)
        tx_cell = f"{marker}{lv.tx_power}"
        row_style = "ok" if is_best else None
        table.add_row(tx_cell, snr_cell, rate_cell, f"{lv.successes}/{lv.samples}", style=row_style)
    return table


def tx_opt_summary(result: TxOptResult) -> Panel:
    """Summarize the result of a TX optimization.

    Args:
        result: The optimization result to summarize.

    Returns:
        A Rich :class:`Panel` that states the optimum that was chosen and if MeshTerm
        applied it.
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

#: The base of the Unicode braille patterns. Add an 8-bit dot mask (a cell of 2×4) to get
#: the glyph.
_BRAILLE_BASE = 0x2800

#: The dot bit for each (column, row) in a braille cell. This is the standard layout of
#: Unicode, and it is the same mapping that the map canvas uses for its rasters. It is
#: here, so that the one-cell battery glyph does not need to reach into the canvas module
#: for two constants.
_BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))

#: The bands of the fuel gauge: ``(percent floor, style)``, with the highest first. The
#: colour answers *how much of the battery is left* in three broad steps: light green on
#: green above half, yellow on brown above a quarter, and light red on red below. The
#: braille fill with eight rungs in the block answers *how much, more exactly*. There are
#: two questions and two channels. A band that stays the same while the dots climb makes
#: the cell readable at a glance, and it is still exact at a second look.
_BATTERY_BANDS: tuple[tuple[int, str], ...] = (
    (50, "batt.full"),
    (25, "batt.mid"),
    (0, "batt.low"),
)

#: The steps in the fill. One braille cell is 2×4 dots, and each row of dots lights from
#: left to right. Thus the cell reads as a ladder with eight rungs, one rung for each 12.5%
#: of charge.
_BATTERY_STEPS = 8

#: Halfway down the last rung, the cell starts to alarm. It does this by dropping the
#: ground, and not by a change of hue. In one frame it shows black dots on red
#: (``batt.flash``), and in the next frame the light red on the bare page
#: (``batt.flash.off``). Each other cell of the gauge of the app is a filled block. Thus a
#: beat with no fill at all is the loudest thing that the palette can say. The threshold
#: is a percentage and not a rung, because half of a rung is not a rung. A rung is
#: ``100 / 8`` = 12.5% of the battery, so its midpoint is 6.25%, where the last dot is half
#: spent.
#:
#: MeshTerm makes the alternation itself, one frame at a time. The blink attribute of the
#: console is not useful, also where a terminal supports it. SGR 5 toggles the
#: *foreground* and leaves the ground, and the ground is the half that matters here. (The
#: framebuffer console of the PicoCalc ignores it completely. We checked this on the
#: handheld, with its lack of bright backgrounds.)
_BATTERY_FLASH_PCT = 100 / _BATTERY_STEPS / 2

#: The frames in the charging sweep: empty → four fills → round again (a loop from the
#: bottom to full).
_CHARGE_FRAMES = 5

#: A full battery reads ``100`` with no ``%``. It is the only charge for which MeshTerm
#: removes the sign, so the reading has three cells, the same as ``99%``.
#:
#: This is important because a battery that is topped off is *between* 99 and 100. The
#: charger stops at full, the battery goes back to 99, the charger starts again, and the
#: companion reports the change at each poll as long as it is plugged in (JP, 2026-09-09).
#: The gauge is pinned to the right edge of the header, and the pulse takes what is left.
#: Thus a number that grew by a cell at the top of the range took that cell from the
#: sparkline. It drew the whole activity history again at a new scale, twice each minute,
#: on a device that was not moving. All other width changes in the range, for example 9 to
#: 10 on the way down, are a boundary that a discharging battery crosses one time and never
#: crosses again. They cost one repaint and need no answer.
#:
#: A bare ``100`` next to a full block reads as a percentage without a sign. The ``%`` does
#: its work at the readings that a user can mistake for something else.
_BATTERY_FULL = "100"


def _braille_fill(steps: int) -> str:
    """A single braille cell that is filled from the bottom by ``steps`` (0–8) half-rows.

    Each row of dots is two steps. The left dot lights first, and then the right dot
    completes the row. Thus the fill climbs in half-rows and not in whole rows, and one
    cell has eight levels that the user can tell apart.
    """
    steps = max(0, min(_BATTERY_STEPS, steps))
    bits = 0
    for step in range(steps):
        row, col = 3 - step // 2, step % 2
        bits |= _BRAILLE_DOTS[col][row]
    return chr(_BRAILLE_BASE + bits)


def _battery_steps(percent: int) -> int:
    """The half-rows that a charge lights: one rung for each 12.5%, and never fewer than one.

    The bands are ``0–12.5`` → one rung … ``87.5–100`` → the full cell. Each band takes its
    lower bound. A battery at its last percent still lights a single dot. Thus the gauge is
    never an empty cell while the battery is still running.
    """
    pct = max(0, min(100, int(percent)))
    return max(1, min(_BATTERY_STEPS, int(pct * _BATTERY_STEPS / 100) + 1))


def _battery_band(percent: int) -> str:
    """The band in which a charge draws (refer to :data:`_BATTERY_BANDS`): the *true* charge.

    The function reads the value from the battery and not from the cell. Thus the charging
    sweep keeps showing what the battery holds, while its fill climbs through frames that
    have no meaning on their own.
    """
    for floor, style in _BATTERY_BANDS:
        if percent >= floor:
            return style
    return "batt.low"


def battery_cell(percent: int, *, charging: bool = False, frame: int = 0) -> Text:
    """The battery gauge of the status bar: one filled braille block, then its ``%``.

    The cell is a lit foreground over its own darker ground. It is a *block*, with a colour
    for each band (:data:`_BATTERY_BANDS`): light green on green above half, yellow on brown
    above a quarter, and light red on red below. In that block, the braille fills from the
    bottom up in eight half-row steps (refer to :func:`_braille_fill`), one rung for each
    12.5% of charge. The left dot of a row lights before the right dot. Thus the block
    answers *approximately how much* from across the room, and the dots answer *exactly how
    much* at a second look. Two live states animate from the ``frame`` counter of the
    caller (it advances one step for each repaint tick). Thus the gauge moves, and no
    plumbing for each frame is necessary.

    * **Charging** replaces the fill. The cell sweeps from empty to full in a loop, so the
      user can see a plugged-in battery climb. The band does *not* sweep with it. The colour
      keeps answering for the real charge, because a frame of the animation has no meaning
      on its own. The number next to the cell does not sweep either, and it stays the true
      percent. The sweep climbs by whole rows of dots, and not by the half-rows of the
      fill. It is an animation for "power is coming in" and not a reading. Its tick is slow
      (one step for each repaint), so five frames show this better than nine.
    * **Below** :data:`_BATTERY_FLASH_PCT` (half of the charge of the last dot, and only
      when nothing charges the battery), the cell alternates between black dots on red and
      the light red on the bare page. When the ground drops for a beat, the change is bigger
      than any change of hue in a palette where each other cell is filled. This is the
      purpose: it is the last warning that a handheld gives before it dies.

    Both states run on each platform. They step at the rate at which that platform
    repaints. On the PicoCalc this is every 2 s, where the header is rebuilt on the idle
    tick in any case. Thus an animation costs only a colour swap in a frame that MeshTerm
    already paints. Neither state is behind ``Platform.effects``, because one of them *is*
    the charging state, and the other is the last warning that a handheld gives before it
    dies.

    A battery at 100% is drawn as **not charging**, for each value of the flag. A full cell
    that stays full is the correct picture, and a charger that is topped off and left
    plugged in must not leave the gauge sweeping forever. The function also removes its
    ``%`` (:data:`_BATTERY_FULL`). Thus the full reading has the same three cells as the 99%
    to which it keeps flipping back, and the header does not change its size under a
    battery on a charger.

    Args:
        percent: The state of charge, 0–100 (clamped).
        charging: ``True`` if the battery takes charge (it drives the fill sweep). The
            function ignores it at 100%.
        frame: A tick that always advances. The function reads only its phase, so any
            integer that increases steadily animates the two live states.

    Returns:
        A Rich :class:`Text`: the coloured block, a space, and ``NN%`` in muted text. The
        one exception is the bare ``100`` of a full battery (:data:`_BATTERY_FULL`). The
        colours of the block are on the span of the glyph alone, so the ground stops at the
        cell.
    """
    pct = max(0, min(100, int(percent)))
    # The sweep climbs by whole rows, so the loop stays easy to read. In both cases, the
    # colour comes from the battery. A sweep frame is an animation and not a reading, and
    # it must not have a colour as if it were a reading.
    sweeping = charging and pct < 100
    steps = (frame % _CHARGE_FRAMES) * 2 if sweeping else _battery_steps(pct)
    color = _battery_band(pct)
    if not sweeping and pct < _BATTERY_FLASH_PCT:
        color = "batt.flash.off" if frame % 2 else "batt.flash"
    # The colours are on the *own span of the glyph* and never on the base style of the Text.
    # The block paints a background, and a base style merges into each span. If the block
    # is the base style, the muted percent goes onto the coloured ground next to the cell.
    out = Text()
    out.append(_braille_fill(steps), style=color)
    out.append(f" {_BATTERY_FULL if pct == 100 else f'{pct}%'}", style="muted")
    return out
