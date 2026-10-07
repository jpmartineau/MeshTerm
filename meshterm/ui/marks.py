# SPDX-License-Identifier: Apache-2.0
"""The shared mark constants: node-type glyphs, their colours, and path sentinels.

The spatial screens of the app (the map, the path graph, the braille rasters, and the
node rows) all mark nodes with the same marks in the same hues. Several of them need the
constants *without* the code around them. Before this module, ``ui.widgets`` put
``map_render``, ``mapcanvas``, and ``pathgraph`` on the boot path for a few tuples and
type aliases. This was approximately 60 ms of import time on the PicoCalc. Now the
constants are here, with no dependencies, and the large modules import *from* the
constants instead of holding them. ``map_render``, ``mapcanvas``, and ``pathgraph``
export their old names again, so the code that imports from those modules does not
change.
"""

from __future__ import annotations

from collections.abc import Callable

#: An ``(r, g, b)`` colour triple, 0 to 255 for each channel (the colour type of a raster
#: or a canvas).
RGB = tuple[int, int, int]


def parse_hex(color: str) -> RGB:
    """Convert ``"#rrggbb"`` (or ``"rrggbb"``) to an ``(r, g, b)`` tuple."""
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


# The palette of markers, shared in the whole app. The basemap is blue (water), green
# (parks), and warm amber (roads). Thus the markers use the hues that a map never has,
# pink and violet, so that they are easy to see against it. Ordinary nodes are the
# loudest (bright pink). Repeaters are less loud (a calmer violet). Our node is always the
# yellow star. A node that was heard but never identified is a light grey ring. Each glyph
# is in the PicoCalc console font.
#
# The colour of a mark is any value that :func:`~meshterm.ui.theme.mark_rgb` takes: a
# literal hex, or the name of a theme style when the platform must choose the shade. The
# unknown ring has a name (``node.unknown``), so that the ring and the grey label beside
# it can never be different.
SELF_MARK = ("★", "#facc15")
REPEATER_MARK = ("▲", "#a78bfa")
NODE_MARK = ("●", "#f472b6")
UNKNOWN_MARK = ("○", "node.unknown")

#: The only concealed character. One bullet replaces one character of a value that the
#: screen hides: a password that the user types (``ui/tui/prompt.py``), or the BLE pairing
#: PIN on the Device info page. The concept has one glyph, so a row of these glyphs has the
#: same meaning where it appears. It is *not* the ``●`` of an unread message or a node, on
#: purpose. Those marks have a meaning, and this one means "not shown".
MASK_MARK = "•"

#: Sentinels that name the endpoints of a path on the route graph. ``\x00`` never occurs
#: in a node hash, so the sentinels can share the namespace of node ids and not collide
#: with an id.
SRC_NODE = "\x00src"
DST_NODE = "\x00dst"

#: Gives the graph marker of a node id: a ``(glyph, colour)`` pair.
GlyphOf = Callable[[str], tuple[str, str]]

#: The graph label of a node, or ``None`` or ``""`` to leave the marker bare.
LabelOf = Callable[[str], str | None]

#: The colour in which the label of a node is drawn (usually the hue of the node name).
LabelRgbOf = Callable[[str], RGB]
