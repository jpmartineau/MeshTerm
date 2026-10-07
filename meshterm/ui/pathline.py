# SPDX-License-Identifier: Apache-2.0
"""Path lines: the flexible text widget for a sequence of hops (powerline chips or arrows).

This widget is the successor to :func:`~meshterm.ui.widgets.path_text` as the only way
that a sequence of hops renders as text. It is used for the walked route of a trace, the
``via`` chain of a packet, the delivery paths of a message, the preview of the composer,
and a row of the trophy case. ``path_text`` is one fixed presentation: the hops are joined
by arrows, the result is one line, and the caller must handle all overflow. A
:class:`PathLine` separates *what the hops are* from *how the line is drawn*. Thus each
surface that the survey found can in the end use it.

* **Two separator styles.** ``plain`` joins the hops with muted ``→`` arrows. This is the
  look that exists today, without a change. ``powerline`` renders each hop as a chip with
  a colour fill. One solid triangle U+E0B0 joins the chips, and it interlocks between
  them. The chip behind it is its foreground, and the chip ahead of it is its background.
  Thus a route reads as a ribbon whose segments meet on a chevron. This is the oh-my-posh
  look, and each seam uses one cell. The label of the chip of a node has the hue that
  comes from its hash (:func:`~meshterm.ui.theme.node_style`). This is the colour that its
  name has on each surface. The fill is that hue, but darker (:data:`CHIP_LIGHTNESS`). Our
  node is the yellow ``★`` of the map on a neutral dark grey. A faded hop is dark slate. A
  keyless hop is light grey on grey. (On the 16-colour console of the PicoCalc, the fill
  is the dim twin of the hue. Refer to :func:`_slot_colours`.)

  Sometimes two chips have the *same* fill (a mirrored return leg, or a stretch of
  keyless greys). Sometimes they have two fills that are too close to tell apart (less
  than :data:`SEAM_BLUR` apart in OKLab, the perceptual distance that
  :mod:`~meshterm.ui.oklab` measures). Then the interlock cannot draw the seam, and the
  seam is the *thin* chevron instead. It has the colour of the label of the previous chip,
  and it is drawn on the next chip. Thus the ribbon continues without a break, and the
  join is a line that the chip draws on itself (:meth:`PathLine._seam`).

  Hops that are elided from the middle break the ribbon and do not join it. The mark is
  bare on the page, between a closing point and the notch of the next chip
  (:func:`elision_hop`). A filled ``⋯`` chip reads as a node with that name. A chip that a
  *cut* catches (a lane that ran out, or a line that scrolled past its edge) breaks off on
  a half block in its own fill (:func:`cut_mark`). This crack shows that the segment
  continues. Only chips crack, and an arrow line still ellipsizes.

  The two *outer* ends of a line read in the same way. A path that begins at its origin and
  ends at its destination opens and closes flat (with a rounded cap where the font has
  one). A path that draws only the *middle* of a route has the chevron at that end
  instead. An example is the ``via`` chain of a packet, which names relays and neither of
  the nodes between which the packet went. The chevron is the same mark that a wrapped
  line uses for "there is more of this out there" (``from_origin`` and
  ``to_destination``).

  A row can also have the ``…`` of the app that shows that the row opens further prompts.
  The widget hangs this mark *outside* the budget (:func:`with_action_mark`), because
  chrome must not use cells of the route. ``auto`` (the default) picks powerline only when
  the terminal can draw it (:func:`~meshterm.ui.termfont.powerline_enabled`: a
  recommended font, a renderer that can draw the glyphs, or the override of the user). In
  all other cases it uses arrows, so no terminal ever shows tofu.
* **Three answers to overflow.** They match the three patterns that the surfaces already
  use:

  * :meth:`PathLine.text` is the full one-line text. It is for callers that crop it with
    :func:`cut_to`, or that read it through a sliding window of their own. :func:`cut_mark`
    marks each cut edge.
  * :meth:`PathLine.ellipsized` fits a width, and it elides hops behind a ``⋯`` mark. It
    eats into the side that the caller names. :data:`ELIDE_TAIL` is the default, and both
    endpoints stay. A plain truncation of the tail removes the destination, and this
    method does not. :data:`ELIDE_HEAD` is for a surface that wants to give up the whole
    head.
  * :meth:`PathLine.wrapped` breaks at the boundaries of hops, under a hanging indent
    (never in the middle of a name, never in the middle of a chip). It folds where the
    route has a meaning. It uses the fewest lines that it can, and it makes them even.
    It does not pack the lines greedily. When the fold costs nothing, it prefers the seam
    where the path changes from composed to mirrored.

  A caller that does not know its width (for example a value in a grid of labels and
  values) gives the :class:`PathLine` itself to Rich. It gets the wrapped shape at the
  cell width that the layout finds (refer to :meth:`PathLine.__rich_console__`).
* **The same hop semantics everywhere.** A :class:`PathHop` has what the conventions of
  the app need:

  * the label (a name, or a hash that stands as the identity),
  * the key, of which any known prefix selects the hue,
  * the ``you`` flag,
  * the ``(3d)`` annotation for traces,
  * the width of the lit prefix for hash labels (the two tones of ``highlighted_hash``),
  * the ``dim`` fade for resolved return legs,
  * an explicit style override for colourings of a context that win over the identity.

  Chips keep exactly the same words as arrows. Only the colours and the separators
  change. Thus a route reads the same in each mode. A surface whose route always starts
  and ends on us can ask for ``bare_self``. This replaces our name and hash with the
  ``★`` of the app. The user already knows who both ends are, and the cells are for the
  hops that *are* news. These stars fade where the route is being *composed*, because our
  two ends are fixtures and not choices. A surface that only *shows* a walk passes
  ``dim_self=False`` and keeps us in the ``you`` white.

The existing call sites still render through ``path_text``. A separate pass for each
surface will move them here. The insertion cursor of the composer is a hop in its own
right (``cursor=True``, drawn as the :data:`CURSOR_GLYPH` slot). An insertion point *is* a
position in the route, and it is not a gap between two positions. Thus it is measured,
wrapped, and coloured like each other hop. The preview keeps the mode that the terminal
earned, and a cursor can never be stranded on a line break.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, RenderResult
from rich.measure import Measurement
from rich.text import Text

from ..core.models import LOCAL_DEVICE_LABEL
from ..platforms import Platform, on_platform
from . import oklab
from .marks import SELF_MARK
from .termfont import powerline_enabled, powerline_full
from .theme import DIM_TWIN, active_theme, node_style, slot_hex

#: The powerline solid right-pointing triangle (U+E0B0). It is the glyph that the widget
#: draws at each seam and closing edge. It is from the *core* set on purpose, so that each
#: recommended font qualifies (the rounded caps at U+E0B4 and later exist only in full Nerd
#: Font patches). A path flows in one direction only, so the point always faces right. What
#: changes is if it lands on a field (a seam) or on the page (an edge).
POWERLINE_SEP = "\ue0b0"

#: The thin right-pointing chevron (U+E0B1). It is the outline sibling of the solid point,
#: in the same core set. It is powerline's own mark for a join *inside* one colour. It
#: draws the seam between two chips whose fills the eye cannot tell apart
#: (:meth:`PathLine._seam`). It is the previous fill, shaded, and drawn on the next chip,
#: where a solid point disappears.
POWERLINE_THIN = "\ue0b1"

#: The rounded caps (U+E0B6 opening, U+E0B4 closing) that finish the two outer ends of a
#: path as a lozenge. They are in the *extended* block, which only a full Nerd Font patch
#: has (:func:`~meshterm.ui.termfont.powerline_full`). A terminal that has only the core
#: four glyphs makes these ends square, and does not show tofu.
POWERLINE_ROUND_OPEN = ""
POWERLINE_ROUND_CLOSE = ""

#: Our node, where a surface asks for it without its name and hash (``bare_self``): the
#: ``★`` of the app. It is the map's own marker, taken from
#: :data:`~meshterm.ui.marks.SELF_MARK`. Thus the star in a route and the star on the map
#: can never be different, in glyph or in hue.
SELF_GLYPH, _SELF_INK = SELF_MARK

#: The insertion cursor of the composer. It stands in the route as a hop of its own: the
#: empty slot into which the next chosen hop goes. It is a ``+``, because this is what
#: Enter does there. Also, no node has this name, so the user cannot read the slot as a hop
#: that is already in the route.
CURSOR_GLYPH = "+"

#: The arrow that joins hops in plain mode, the same as ``path_text`` draws it today.
_ARROW = " → "

#: The theme name that a rendered path line has as its base style. It draws *nothing*, and
#: it exists so that code can recognize it. Each shape that this widget makes has it (refer
#: to :meth:`PathLine._render`). Thus a row that appends a path has a span that says where
#: the route is. The identity fold of the cursor row uses this to spare the hop hues
#: (:func:`~meshterm.ui.tui.render._whiten_identities`), and it does not make a whole route
#: white.
PATH_INK = "pathline"

#: The lightness of the chip of a node, as a part of the lightness of its hue (OKLab ``L``).
#: On a truecolor terminal, the fill of the chip of a node is its hue with ``L`` multiplied
#: by this value, and the label on it is the hue itself. This is the colour that the name
#: has in each list and in arrow mode. Thus a node has one colour on each surface, and a
#: chip only puts a darker ground of that colour under it. JP chose this look on the
#: PicoCalc (2026-10-06). There, the dim twin of a slot is the same rule at the resolution
#: of the console (:data:`~meshterm.ui.theme.DIM_TWIN`). Its dim twins have 0.55 to 0.73 of
#: the lightness of their bright slots.
#:
#: The value came from two ladders of rules over twelve hues. A fixed step down in ``L``
#: made the blue fills almost black, because the blue hue is the darkest on the wheel. One
#: ``L`` for all fills made the blue label hard to read on its fill. ``0.65`` made the blue
#: label weak, and ``0.55`` made the yellow fills dull. The fill also keeps its hue when
#: the darker colour leaves the sRGB gamut, because the chroma comes down (refer to
#: :func:`~meshterm.ui.oklab.fitted`). A clip there made the greens near ``0x50`` one
#: colour.
CHIP_LIGHTNESS = 0.60


def _chip_fill(colour: str) -> str:
    """The fill of a chip whose label is ``colour``: the same hue, darker."""
    lightness, a, b = oklab.from_hex(colour)
    return oklab.to_hex(oklab.fitted((lightness * CHIP_LIGHTNESS, a, b)))


def _chip_label(fill: str) -> str:
    """The label for a chip whose fill is ``fill``: the inverse of :func:`_chip_fill`."""
    lightness, a, b = oklab.from_hex(fill)
    return oklab.to_hex(oklab.fitted((min(1.0, lightness / CHIP_LIGHTNESS), a, b)))


#: The text of the composer's slot: near-black slate on the white chip.
_CHIP_FG = "#0f172a"
#: The chip fill for a keyless hop: the ``faint`` grey. Colour is only for identities that
#: have a key, in chips and in arrow mode.
_KEYLESS_BG = "#64748b"
#: The label of a keyless chip: the same pair as the chip of a node, in grey.
_KEYLESS_FG = _chip_label(_KEYLESS_BG)
#: The chip fill of our node: a neutral dark grey. Thus the yellow ``★`` on it reads as the
#: marker of the map and not as the hue of a node (JP, 2026-08-09). It is *not* a colour
#: of the spectrum on purpose, and it is not the white that it was before. Our end of a
#: route is a fixture that the user already knows. It stays back and lets the hops that
#: differ carry the colour.
_YOU_BG = "#3f3f46"
#: The chip of a dimmed hop: a dark slate fill with muted ink. It recedes like ``faint``
#: text.
_DIM_BG = "#334155"
_DIM_FG = "#94a3b8"

#: On the 16-colour console, the page: slot 0, black. It is the fill of a chip that the
#: console cannot fill, because the VT has no dark grey background. That chip is our ``★``,
#: a faded hop, or the slot of the composer. Its text then sits on the page, and the chips
#: on each side close on a point and open on a notch.
_SLOT_PAGE = slot_hex(0)
#: On the console, the text of a faded hop: slot 8, the dark grey.
_SLOT_FAINT = slot_hex(8)
#: On the console, white: slot 15. It is the text of a keyless chip and of the slot of the
#: composer.
_SLOT_WHITE = slot_hex(15)


class _ChipColours(NamedTuple):
    """The colours of one chip.

    Attributes:
        fill: The background of the chip.
        ink: The colour of the label.
        soft: The colour of an annotation, and of a hash label after its lit bytes.
    """

    fill: str
    ink: str
    soft: str


#: The mark for elided hops (refer to :meth:`PathLine.ellipsized`). It renders as a dim
#: pseudo-hop, so it recedes in both modes.
_ELISION = "⋯"

#: The classic truncation mark, which an *arrow* line still uses to cut: three dots where
#: words were. An arrow hop is text, so a shorter text is what happened.
_ELLIPSIS = "…"

#: The mark of the app for "this row opens further prompts" (refer to the UX standards), as
#: a *path* row has it. Refer to :func:`with_action_mark` for the reason that the module
#: spells it here, and each surface does not append it.
ACTION_MARK = " …"

#: What a *chip* line uses to cut instead (JP, 2026-08-09): the half block. It is drawn in
#: the cut chip's own fill, with no background. Thus the cell is half segment and
#: half bare page. The chip breaks off in the middle of its body, and the crack says that
#: it is incomplete. An ellipsis says the opposite: that a *word* was made shorter, when
#: what ran out is the lane. :data:`CRACK_TAIL` closes a line past which the path runs on
#: (the left half of the fill stays, and the page takes the right half).
#: :data:`CRACK_HEAD` is its mirror, and it opens a line whose start is off the screen.
#: Only chips crack. :func:`cut_mark` finds a fill, or it uses :data:`_ELLIPSIS`. Thus this
#: idea does not change arrow lines.
CRACK_TAIL = "▐"
CRACK_HEAD = "▌"

#: The side of a path into which a fit of :meth:`PathLine.ellipsized` eats. ``ELIDE_TAIL``
#: is the reading for a route. The origin anchors the line, the destination is saved from
#: the far end, and the mark lands in the middle. ``ELIDE_HEAD`` is the reading for a
#: trail. The last hop anchors the line, and everything before it goes, also the origin.
ELIDE_TAIL = "tail"
ELIDE_HEAD = "head"

#: The extra columns that a wrapped line steps in after the hanging indent. The marks of
#: continuation (a trailing ``→``, a chip edge with a notch) say that the path goes on.
#: The step says it at a glance, from the shape of the block alone. A route that folds
#: reads as one value that ran long, and not as a second value under the first.
WRAP_OFFSET = 2


@dataclass(frozen=True)
class PathHop:
    """One hop of a path line: the semantics, and not how the hop is drawn.

    Attributes:
        label: What the line shows. It is a resolved name, a hash that stands as the
            identity of the node (in that case, set ``lit_bytes`` for the prefix in two
            tones), or the bare :data:`SELF_GLYPH` where a surface does not need to name us.
        key: Any known prefix of the key or hash of the node. It selects the hue that
            comes from the hash (its first byte, so each prefix gives the same hue).
            ``None`` renders muted and grey, because colour is only for identities that
            have a key.
        you: Our node. Arrows draw it in the pure-white ``you`` style. Chips draw it in the
            own yellow of the map on the neutral dark grey :data:`_YOU_BG`, with padding
            like each other chip.
        annotation: The hash note for traces. It is rendered ``" (3d)"`` after the label in
            both modes (pass the bare ``3d``, without parentheses).
        lit_bytes: For a hash label: the leading *bytes* that are drawn in the hue (the two
            tones of ``highlighted_hash``). The rest is muted or soft. ``0`` for names.
        dim: Fade the hop (and the arrow that leads into it). It is the resolved return leg
            of a planned route, which the user does not compose.
        style: An explicit colour override. It is a hex style (``"#rrggbb"``, attributes are
            permitted) or a theme name. It is for colourings of a context that win over the
            identity. Plain mode uses it as the label style, and powerline uses it as the
            chip fill.
        cursor: This hop is an insertion slot of an editor, and not a node. It is the
            :data:`CURSOR_GLYPH` in the ``cursor`` white of the app. The list cursor
            (``❯``) below it has the same colour. Thus the two halves of one gesture (the
            row that the user selects, and the slot where it lands) read as one thing.
        gap: This hop is a mark *between* two nodes, and it is not a node. It is the elision
            (:func:`elision_hop`). Chips draw it in the break between two segments, bare on
            the page, with no fill and no padding. It says "the ribbon stops here and
            continues again". A chip says the opposite: that a node in the route has the
            name ``⋯``. Arrow mode does not need a special case, because a bare label
            between two arrows is already the same thing.
    """

    label: str
    key: str | None = None
    you: bool = False
    annotation: str | None = None
    lit_bytes: int = 0
    dim: bool = False
    style: str | None = None
    cursor: bool = False
    gap: bool = False


def elision_hop() -> PathHop:
    """The mark for hops that MeshTerm removed from the middle (or the head) of a path.

    This is the only way to say "hops were left out here". Thus each surface that makes a
    path shorter uses the same glyph, *and* the same fact that it is a gap and not a node
    (refer to :attr:`PathHop.gap`).
    """
    return PathHop(_ELISION, dim=True, gap=True)


def _style_hex(style: str) -> str | None:
    """The ``#rrggbb`` colour to which a style string or theme name resolves, else ``None``."""
    for token in reversed(style.split()):
        if token.startswith("#") and len(token) == 7:
            return token
    themed = active_theme().styles.get(style)
    if themed is not None and themed.color is not None:
        try:
            return "#" + themed.color.get_truecolor().hex.lstrip("#")
        except Exception:
            return None
    return None


#: The distance that two chip fills must have, as the eye measures it, for the seam between
#: them to interlock. If they are closer than this, the solid point is on a field from
#: which the eye cannot tell it, so the seam is the thin chevron instead
#: (:meth:`PathLine._seam`). The unit is the OKLab distance
#: (:func:`~meshterm.ui.oklab.distance`). ``0.02`` is the smallest difference that a user
#: can see between two large patches, and ``1`` is from black to white.
#:
#: The rule that this value replaced was first key bytes less than ``0x10`` apart on the
#: hue wheel (JP, 2026-09-15). That rule measures ``0.087`` at the median hue, but ``0.026``
#: across the greens and ``0.165`` across the cyans. The wheel is not uniform to the eye.
#: Thus a gap of hue blurred too little there and too much here. An sRGB distance only
#: restates the hue gap (a fixed step of one byte is a near-constant ``55`` to ``60`` of 8-bit
#: RGB all the way round). Then JP set the value by eye, on a ladder of pairs from the
#: spectrum, drawn in both ways (JP, 2026-09-16). The value is where the solid point is not
#: only visible at one cell, but the user *finds* it without a search: ``0.06``, which is
#: three differences that the user can see. A tighter value (``0.035``) left wedges that
#: the user must search for. A looser value (``0.10``) blurred pairs whose point still read
#: clearly. ``0.05`` was a little too tight. ``0x10`` of green is ``0.026`` and blurs.
#: ``0x10`` of red is ``0.124`` and does not blur. The two greys, which are ``0.18`` apart,
#: keep their interlock by the metric alone.
SEAM_BLUR = 0.06


def _fills_blur(before: str, after: str) -> bool:
    """Check if two fills look the same: identical, or less than :data:`SEAM_BLUR` apart."""
    return before == after or oklab.distance(before, after) < SEAM_BLUR


#: The fill of a chip, as it appears in the style of a rendered span: the ``on #rrggbb``
#: half. Only :meth:`PathLine._chip` sets a background. Thus this is the test for "is there
#: a chip here". The point of a seam, a notch, and each span in arrow mode have a
#: foreground alone.
_ON_FILL = re.compile(r"\bon\s+(#[0-9a-fA-F]{6})\b")


def _fills(line: Text) -> list[str | None]:
    """The chip fill under each *character* of a rendered line (``None`` if not on a chip)."""
    fills: list[str | None] = [None] * len(line.plain)
    for span in line.spans:
        style = span.style if isinstance(span.style, str) else str(span.style)
        found = _ON_FILL.search(style)
        if found is None:
            continue
        for i in range(max(0, span.start), min(len(fills), span.end)):
            fills[i] = found.group(1)
    return fills


def _char_of_cell(plain: str, cell: int) -> int:
    """The index of the character in which a display cell falls, limited to the string."""
    if cell <= 0:
        return 0
    at = 0
    for i, ch in enumerate(plain):
        at += cell_len(ch)
        if at > cell:
            return i
    return max(0, len(plain) - 1)


def cut_mark(line: Text, at: int, side: str) -> Text:
    """The one cell that says that a rendered path line was cut here.

    A chip that the cut catches breaks off in its own colour (:data:`CRACK_TAIL` and
    :data:`CRACK_HEAD`). Half of the cell is still segment, and half is bare page. Thus the
    user sees a chip that *continues*, and not a route that ended. A line that is drawn in
    arrows has no fill to shear, so it cuts the classic way, with :data:`_ELLIPSIS`. This
    fallback is the whole test of the mode, and the caller has no cost to ask for it.

    The function reads the fill from the nearest cell *inside* the line. For a tail cut it
    scans back from ``at``, and for a head cut it scans forward. Thus a cut that lands on a
    seam or on the closing edge still cracks in the colour of the chip that the user can
    see, and not in nothing.

    Args:
        line: The rendered line that MeshTerm cuts (whole, not shifted, with absolute cells).
        at: The index of the nearest *visible* cell on the side of the mark. For
            :data:`ELIDE_TAIL` it is the last cell that is still drawn. For
            :data:`ELIDE_HEAD` it is the first.
        side: The end of the line that the mark closes. :data:`ELIDE_TAIL` (the path runs
            on to the right) or :data:`ELIDE_HEAD` (it began off to the left).

    Returns:
        The styled single cell, ready to append or prepend.
    """
    fills = _fills(line)
    if not fills:  # there is nothing drawn to shear: a caller cut an empty line
        return Text(_ELLIPSIS, style="muted")
    index = _char_of_cell(line.plain, at)
    steps = range(index, len(fills)) if side == ELIDE_HEAD else range(index, -1, -1)
    fill = next((fills[i] for i in steps if fills[i]), None)
    if fill is None:
        return Text(_ELLIPSIS, style="muted")
    return Text(CRACK_HEAD if side == ELIDE_HEAD else CRACK_TAIL, style=f"{_FLAT}{fill}")


def with_action_mark(line: Text, width: int) -> Text:
    """``line`` plus the ``…`` for "opens further prompts". The mark goes *past* the width budget.

    The mark says what Enter does on the row. It is not part of the path, so it must not
    cost the path a cell. If the function kept room for it first, a route that fitted its
    lane exactly cracked on its last chip (JP, 2026-08-10). Then the row claimed that the
    walk ran on, when the walk was in fact finished. The crack replaces only the closing
    cap, and the two cells that the mark needed were the two cells that the user lost.

    So the path is fitted to the *whole* lane first, and the mark lands in the space that
    is left. If no space is left, the function does not draw the mark. If the user loses
    the mark, the user loses a hint that the behaviour of the row gives when the user
    presses Enter. If the user loses the tail of a hop, the user loses the route. This is
    the default answer of the widget in each place where a path row has the mark. It is not
    a local trade of one surface. Pass the *fitted* line here (after :func:`cut_to` or after
    a scroll window), and let the space that is left decide.

    Args:
        line: The path line, already fitted to ``width``.
        width: The budget of cells for the lane.

    Returns:
        The line with the mark appended, or the line unchanged when the lane has no room
        for the mark.
    """
    if line.cell_len + cell_len(ACTION_MARK) > width:
        return line
    out = line.copy()
    out.append(ACTION_MARK, style="muted")
    return out


def cut_to(line: Text, width: int, *, action: bool = False) -> Text:
    """Fit a rendered path line to ``width`` by a cut of its tail. The cut cracks, not elides.

    This function replaces ``Text.truncate(width, overflow="ellipsis")`` where the line
    that MeshTerm cuts is a path. The function crops the body one cell short, and the free
    cell has :func:`cut_mark`. Thus chips break off in their own colour, and arrow lines
    end in the same ``…`` as always. A line that already fits is returned unchanged.

    Args:
        line: The rendered line to fit.
        width: The budget of cells.
        action: Append the mark for "opens further prompts" afterwards, outside the budget
            (:func:`with_action_mark`). It is for a row from which Enter opens something.

    Returns:
        A line that is not wider than ``width``.
    """
    if width <= 0:
        return Text()
    if line.cell_len <= width:
        fitted = line
    else:
        mark = cut_mark(line, width - 2, ELIDE_TAIL)
        fitted = line.copy()
        fitted.truncate(max(0, width - mark.cell_len), overflow="crop")
        fitted.append_text(mark)
    return with_action_mark(fitted, width) if action else fitted


def hops_atom(count: int) -> Text:
    """The hop count, as the line of statistics under a path line spells it.

    Where a surface puts statistics under a path, the number of hops is one of them (JP,
    2026-08-10). The picture above encodes this number but never states it. Users often
    compare two routes by this number, and without it the user must count the chips. The
    text is ``direct`` where there are no hops. A path with no relays is not "0 hops". It
    is the app's own word for a packet that went straight there (the ``empty`` note
    of :class:`PathLine` says the same).

    Args:
        count: The number of *relay* hops that the route took. The endpoints are not
            counted. Us and the far node are on each route here, so a count of them says
            nothing.

    Returns:
        The muted atom, ready to join a chain of ``·``.
    """
    if count <= 0:
        return Text("direct", style="muted")
    return Text(f"{count} hop" if count == 1 else f"{count} hops", style="muted")


class PathLine:
    """A sequence of hops that renders as text joined by arrows, or as powerline chips.

    Build one from :class:`PathHop` entries. Then ask for the shape that the surface
    needs: :meth:`text` (the full line), :meth:`ellipsized` (elided in the middle to a
    width), or :meth:`wrapped` (lines that break at hop boundaries, under a hanging
    indent).

    Args:
        hops: The hops in the order of display. The widget never changes the order. A
            reversed or mirrored walk is the composition of the caller.
        mode: ``"auto"`` (powerline when the terminal can draw it, the default),
            ``"powerline"``, or ``"plain"``.
        separator: The joiner for plain mode. The default is the ``" → "`` of the app. A
            surface that has its own convention (the trail of the mesh walk uses
            ``" › "``) can pass it. Powerline mode ignores it.
        empty: The muted text that a path with no hops reads as (``"direct"``).
        from_origin: The first hop *is* the origin of the route. Clear it where the line
            draws only the middle of a route. (The ``via`` chain of a packet names its
            relays, and neither the node that sent it nor the node that received it.)
            Then the head is drawn as a continuation and not as a beginning.
        to_destination: The last hop *is* the destination of the route. Clear it in the
            same case, at the other end.
    """

    def __init__(
        self,
        hops: Sequence[PathHop],
        *,
        mode: str = "auto",
        separator: str = _ARROW,
        empty: str = "direct",
        from_origin: bool = True,
        to_destination: bool = True,
    ) -> None:
        """Hold the hops and the drawing choices. MeshTerm does not measure or draw yet.

        The class docstring above describes each argument.
        """
        self._hops = list(hops)
        self._mode = mode
        self._separator = separator
        self._empty = empty
        self._from_origin = from_origin
        self._to_destination = to_destination

    @property
    def hops(self) -> list[PathHop]:
        """The hops, for callers that join lines (endpoints and relays)."""
        return list(self._hops)

    # --- the three shapes ---------------------------------------------------------

    def text(self) -> Text:
        """The full one-line rendering. For overflow, the caller decides for each surface.

        Returns:
            A one-line :class:`Text` (the ``empty`` note when there are no hops).
        """
        if not self._hops:
            return Text(self._empty, style="muted")
        return self._render(self._hops)

    def ellipsized(self, width: int, *, elide: str = ELIDE_TAIL) -> Text:
        """The line fitted to ``width`` by the elision of hops behind a ``⋯`` mark.

        ``elide`` names the *side* into which the mark eats. It decides which end of the
        path pays for the overflow:

        * :data:`ELIDE_TAIL` (the default) anchors the line on its head. The origin stays,
          and hops go from the tail end. The function saves the destination from that side,
          with as much of the run before it as fits. Thus the ``⋯`` settles in the
          *middle*, and both endpoints stay. A route reads the origin and the destination
          first, and a plain truncation on the right removes the second. The function gives
          up the origin only if even this does not fit. As the last resort, a single hop
          that is too long is cut where the width runs out (:func:`cut_to`: a chip cracks
          off in its own colour, and an arrow line ellipsizes).
        * :data:`ELIDE_HEAD` anchors the line on its tail. The last hop stays, and hops go
          from the head end, with the origin among them. The function saves no endpoint. The
          news of a breadcrumb trail is where the walk *is*. Where it started is the part
          that is best to lose.

        Args:
            width: The budget of cells in which the returned line must fit.
            elide: The side into which the ``⋯`` eats. :data:`ELIDE_TAIL` (the default)
                keeps both endpoints, and :data:`ELIDE_HEAD` keeps the tail alone.

        Returns:
            A one-line :class:`Text` that is not wider than ``width``.
        """
        full = self.text()
        if full.cell_len <= width or not self._hops:
            return full
        mark = elision_hop()
        count = len(self._hops)
        # The heads to try, with the most saved first. An elision on the tail side spares
        # the origin while it fits, and gives it up only when it must. An elision on the
        # head side eats the head on purpose, so it never spares the origin.
        for head in (0,) if elide == ELIDE_HEAD else (1, 0):
            for tail in range(count - 1 - head, 0, -1):
                kept = self._hops[:head] + [mark] + self._hops[-tail:]
                candidate = self._render(kept)
                if candidate.cell_len <= width:
                    return candidate
        last = self._render([mark, self._hops[-1]]) if count > 1 else full
        return cut_to(last, width)

    def wrapped(self, width: int, *, indent: int = 0) -> list[Text]:
        """The line broken at hop boundaries, with a hanging indent of ``indent`` columns.

        This follows the convention of the hanging indent. The caller puts its label lane
        on the first line, so the function returns that line *without* the indent prefix.
        Each continuation starts with ``indent`` spaces, plus :data:`WRAP_OFFSET`. This step
        makes a fold clear as a fold. Each line fits in ``width``.

        Plain-mode lines that continue end with the separator's own mark: a trailing
        ``→`` for a path that arrows join, or the bare ``,`` for a wire spec that commas
        join. This is the cue "the path goes on". Chip lines always close with their pointed
        edge. From the second line down, they open when they draw again the seam that the
        break interrupted: the point that arrives out of the last fill of the previous line.
        Thus the user can never mistake a continuation for a path that starts again. A
        single hop that is wider than the content column stands alone, and :func:`cut_to`
        cuts it to the column: a chip breaks off at the crack, and an arrow hop ellipsizes.

        MeshTerm chooses where the breaks fall by how the result *reads* (refer to
        :meth:`_flow` and :meth:`_turn_seam`), and does not pack each line full. The fold
        takes the fewest lines that the hops allow, and spreads the hops evenly across them.
        It lands on the turn of the route, where a leg gives way to its mirrored return,
        when a fold there costs no extra line.

        Args:
            width: The full budget of the line, with the indent.
            indent: The column of the hanging indent, under which the continuations align.

        Returns:
            The lines, in order (a single ``empty`` note when there are no hops).
        """
        if not self._hops:
            return [Text(self._empty, style="muted")]
        budget = max(1, width - indent)
        plain = self._resolved_mode() != "powerline"
        # The continuation cue is the separator's own mark, without the space where the
        # next hop sat: " → " reads " →", and the "," of a wire spec stays ",".
        cue = self._separator.rstrip() if plain else ""
        groups = self._flow(self._hops, budget, plain, len(cue), 0)
        seam = self._turn_seam()
        if seam is not None and len(groups) > 1:
            # The last line of the outbound leg still continues into the return, so it
            # keeps the cue, but the final line of the whole path does not.
            legs = self._flow(self._hops[:seam], budget, plain, len(cue), len(cue), ends=False)
            legs += self._flow(self._hops[seam:], budget, plain, len(cue), 0, carry_in=True)
            if len(legs) <= len(groups):  # the fold at the turn costs nothing, so use it
                groups = legs

        lines: list[Text] = []
        for i, group in enumerate(groups):
            step = 0 if i == 0 else WRAP_OFFSET
            line = Text() if i == 0 else Text(" " * (indent + step))
            body = self._render(
                group,
                force_plain=plain,
                carry_in=bool(i),
                carry_on=i < len(groups) - 1,
            )
            # MeshTerm truncates a lone hop that is too wide for the column. It leaves room
            # for the cue that the line must still carry, so that line also stays inside the
            # width. If the column is too narrow for both, the line drops the cue, because
            # content wins the cells.
            continues = i < len(groups) - 1 and 0 < cell_len(cue) < budget
            room = budget - step - (cell_len(cue) if continues else 0)
            if body.cell_len > room:
                body = cut_to(body, room)
            line.append_text(body)
            if continues:
                line.append(cue, style="muted")
            lines.append(line)
        return lines

    # --- the shape that a Rich layout asks for ------------------------------------

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render into a Rich layout (a table cell, a group) as the wrapped shape.

        This is the one shape for which the caller does not have to find the size. The
        caller can put the line directly into a grid of labels and values (the ``via`` row
        of the packet viewer). The line folds at hop boundaries to the cell width that the
        layout finds for the value column. It hangs its continuations under the value block
        in the same way as :meth:`wrapped` lays them out. Thus a surface whose lane width
        depends on the other rows does not have to compute it twice. A caller that already
        knows its width can still ask for a shape directly.
        """
        yield from self.wrapped(max(1, options.max_width))

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        """The width that the line wants: from the whole one-line text down to the cells of one hop.

        The maximum is the unwrapped line. If there is room, a route reads best without a
        fold. The minimum is the widest single hop plus the chrome that each line has (the
        closing edge of a chip, the seam that a continuation opens again, and the
        :data:`WRAP_OFFSET` step). Below this value, no column can hold a hop whole. Thus a
        narrower lane means that the layout chooses to crop and not to wrap.
        """
        full = self.text().cell_len
        if not self._hops:
            return Measurement(full, full)
        cells, _join, tail, lead, _head, _foot = self._measure(
            self._hops, self._resolved_mode() != "powerline"
        )
        least = max(cells) + tail + lead + WRAP_OFFSET
        return Measurement(min(least, full), full)

    # --- where the breaks fall --------------------------------------------------------

    def _measure(
        self, hops: list[PathHop], plain: bool
    ) -> tuple[list[int], int, int, int, int, int]:
        """The width of each hop, plus the cost to join, open, and close a line.

        After MeshTerm measures each hop one time, the fit of hops to a column is
        arithmetic. This is important because :meth:`_flow` tries a whole series of columns,
        and the screens fit again at each repaint (also at the tick of a spinner).

        Args:
            hops: The hops to measure, in order.
            plain: Measure as text with arrows and not as chips.

        Returns:
            ``(cells, join, tail, lead, head, foot)``. These are the cells of each hop. Then
            the cells that one join between two hops costs. (In chip mode, the seam is the
            single interlocked chevron. This is also true on each side of an elision gap, so
            the figure is correct there too.) Then the cells with which a line closes, when
            the path *runs on past* it (the point in chip mode, and nothing in arrow mode).
            Then the cells with which each line *after the first* opens (the notch in chip
            mode, which is the seam drawn again). The last two are the outer caps: what the
            *first* line opens with, and what the line that *ends* the path closes with.
            Each is one cell where the font has the rounded caps and nothing where it does
            not have them. MeshTerm draws a square end when it appends nothing.
        """
        if plain:
            plain_cells = [self._plain_hop(hop).cell_len for hop in hops]
            head = 0 if self._from_origin else cell_len(self._separator.lstrip())
            foot = 0 if self._to_destination else cell_len(self._separator.rstrip())
            return plain_cells, cell_len(self._separator), 0, 0, head, foot
        widths = [
            cell_len(hop.label) if hop.gap else self._chip(hop, _colours_of(hop)).cell_len
            for hop in hops
        ]
        sep = cell_len(POWERLINE_SEP)
        cap = cell_len(POWERLINE_ROUND_OPEN) if powerline_full() else 0
        # An end that the route runs on past costs its chevron, for any font. The point and
        # the notch are core glyphs, and the *cap* is optional. Thus a line with only relays
        # is a cell wider at that end than a whole route, on a terminal that has no rounded
        # caps.
        head = cap if self._from_origin else sep
        foot = cap if self._to_destination else sep
        return widths, sep, sep, sep, head, foot

    @staticmethod
    def _fill(
        cells: list[int],
        join: int,
        tail: int,
        budget: int,
        reserve: int,
        last: int,
        lead: int = 0,
        head: int = 0,
        foot: int = 0,
        ends: bool = True,
    ) -> list[int]:
        """Pack the hop widths into lines of ``budget`` cells. It is greedy and never splits a hop.

        Args:
            cells: The width of each hop, in order (refer to :meth:`_measure`).
            join: The cells that one join between two hops costs.
            tail: The cells with which a line closes, when the path runs on past it.
            budget: The width of the content column in cells.
            reserve: The cells that the function keeps on a line for the continuation cue
                that the line will have.
            last: The cells that the function keeps on a line that ends at the *final* hop.
                It is ``0`` when the path really ends there (nothing follows, so there is
                no cue). It is ``reserve`` when this is only one leg of a path that goes on.
            lead: The cells with which each line but the first opens: the seam that opens
                again, plus the :data:`WRAP_OFFSET` step by which it hangs past the indent.
            head: The cells with which the first line opens (the rounded cap, when it is
                drawn).
            foot: The cells with which the line that *ends the path* closes (the rounded
                cap, when it is drawn). The function charges it instead of ``tail``. A
                finished path closes on a cap, or on nothing at all where the font has no
                cap.
            ends: These hops finish the path, so their last line pays ``foot``. ``False``
                where they are one leg of a route that goes on. Then each line, also the
                last, still closes on the point.

        Returns:
            The number of hops that each line takes. A hop that is too wide for ``budget``
            gets a line of its own (the caller truncates it).
        """
        sizes: list[int] = []
        count = 0
        width = head
        for i, cell in enumerate(cells):
            final = i == len(cells) - 1
            grown = width + cell + (join if count else 0)
            held = last if final else reserve
            close = foot if final and ends else tail
            if count and grown + close + held > budget:
                sizes.append(count)
                width, count = lead + cell, 1
            else:
                width, count = grown, count + 1
        sizes.append(count)
        return sizes

    def _flow(
        self,
        hops: list[PathHop],
        budget: int,
        plain: bool,
        reserve: int,
        last: int,
        carry_in: bool = False,
        ends: bool = True,
    ) -> list[list[PathHop]]:
        """Break ``hops`` into the fewest lines, then make those lines even.

        Greedy packing alone leaves a widow at the tail. If the last hop of a route misses
        the first line by a cell or two, ``us`` is alone under a full line. The *number* of
        lines costs screen rows, and greedy packing already gives the minimum. Thus the
        function packs the same number of lines again, at the narrowest column that still
        needs it. This spreads the hops across the lines, and it does not pack the first
        line full and starve the last. For example, ``us → a → b → c →`` / ``us`` becomes
        ``us → a →`` / ``b → c → us``.

        Args:
            hops: The hops to break up, in order.
            budget: The width of the content column in cells.
            plain: Render as arrows and not as chips.
            reserve: The cells that the function keeps on a line for the continuation cue
                that the line has.
            last: The cells that the function keeps on the line on which the hops end
                (refer to :meth:`_fill`).
            carry_in: These hops are a *later* leg of a path that is already in progress.
                Thus their first line also opens as a continuation, and not as a beginning.
            ends: These hops finish the path, so their last line closes on the outer cap
                and not on the continuation point (refer to :meth:`_fill`).

        Returns:
            The hops, grouped for each line.
        """
        cells, join, tail, lead, head, foot = self._measure(hops, plain)
        lead += WRAP_OFFSET  # each continuation steps in past the hanging indent
        if carry_in:
            head = lead

        def fill(column: int) -> list[int]:
            return self._fill(cells, join, tail, column, reserve, last, lead, head, foot, ends)

        lines = len(fill(budget))
        low, high = 1, budget
        while low < high:  # the narrowest column that still takes `lines` rows
            mid = (low + high) // 2
            if len(fill(mid)) <= lines:
                high = mid
            else:
                low = mid + 1
        groups: list[list[PathHop]] = []
        at = 0
        for size in fill(low):
            groups.append(hops[at : at + size])
            at += size
        return groups

    def _turn_seam(self) -> int | None:
        """The index of the hop where a fold reads best, where the route turns, or ``None``.

        Two things mark a turn. One is a dimmed *tail*: the composed outbound leg ends and
        the mirrored return begins (refer to :attr:`PathHop.dim`). The function reads it as
        the run of faded hops that reaches the end of the line, and not only the first faded
        hop. The opening ``★`` of a route also fades (nobody chooses to set out from us
        either), and that single fixture at the head marks nothing. The other is a walked
        boomerang. Nothing is dimmed after a trace has answered, but the sequence of hops is
        its own mirror, so its middle *is* the same turn. If the plan and the walk that
        answered it fold in the same place, the two readings of one route look like one
        route. In both cases, the fold needs real legs on both sides, with at least two hops
        each. Thus a path whose only faded tail is the automatic return to us never leaves
        that hop as a widow on a line of its own. That single hop marks no turn, and the walk's
        own mirror can still name one.
        """
        seam: int | None = len(self._hops)
        while seam and self._hops[seam - 1].dim:
            seam -= 1
        if seam == len(self._hops):  # nothing is faded at the tail
            seam = None
        if seam is None or len(self._hops) - seam < 2:
            marks = [(hop.label, hop.annotation) for hop in self._hops]
            if len(marks) % 2 and marks == marks[::-1]:
                seam = len(marks) // 2 + 1  # the first hop of the way home
        if seam is None or seam < 2 or len(self._hops) - seam < 2:
            return None
        return seam

    # --- rendering ----------------------------------------------------------------

    def _resolved_mode(self) -> str:
        """The effective mode: ``auto`` resolves with the verdict for the terminal."""
        if self._mode != "auto":
            return self._mode
        return "powerline" if powerline_enabled() else "plain"

    def _render(
        self,
        hops: list[PathHop],
        *,
        force_plain: bool = False,
        carry_in: bool = False,
        carry_on: bool = False,
    ) -> Text:
        """Join ``hops`` in the effective mode, and mark the result as a path line.

        The two carry flags mark the ends of a wrapped line. They have a meaning only for
        chips. Arrow mode says the same thing with the trailing cue.

        Each shape leaves through this function, so here the line stamps its own extent. The
        base style of the returned text is :data:`PATH_INK`, which draws nothing and says
        "these cells are a route". A row that appends it (``Text.append_text``) carries that
        stamp as a span over the hops, and the identity fold of the cursor row reads it there
        (refer to :func:`~meshterm.ui.tui.render._whiten_identities`). In a path line, the
        hue is not a decoration on a name. It shows the difference between one hop and the
        next. Thus the highlight lights the row and does not remove the hues.
        """
        if not force_plain and self._resolved_mode() == "powerline":
            line = self._render_chips(hops, carry_in=carry_in, carry_on=carry_on)
        else:
            line = self._render_plain(hops, carry_in=carry_in, carry_on=carry_on)
        line.style = PATH_INK
        return line

    def _render_plain(
        self, hops: list[PathHop], *, carry_in: bool = False, carry_on: bool = False
    ) -> Text:
        """The hops, joined by arrows: the presentation of ``path_text``, hop by hop.

        An end that the route runs on past has the separator with nothing on the far side of
        it. It is a leading ``→`` where the origin is not drawn, and a trailing ``→`` where
        the destination is not drawn. Arrow mode already spells "the path goes on" with that
        trailing mark when a line wraps, so a chain of relays says it in the same way. Thus
        the two modes tell the user the same thing, on a console that has no chevrons to
        shear (refer to :meth:`_render_chips`). The wrap flags win where they overlap. A
        continuation already opens under its predecessor, and :meth:`wrapped` appends the
        cue itself on a line that the path outruns.
        """
        text = Text()
        if hops and not carry_in and not self._from_origin and not hops[0].gap:
            text.append(self._separator.lstrip(), style="muted")
        for i, hop in enumerate(hops):
            if i:
                text.append(self._separator, style="faint" if hop.dim else "muted")
            text.append_text(self._plain_hop(hop))
        if hops and not carry_on and not self._to_destination and not hops[-1].gap:
            text.append(self._separator.rstrip(), style="muted")
        return text

    def _plain_hop(self, hop: PathHop) -> Text:
        """One hop in arrow mode: the label in its identity style, and a muted annotation."""
        note_style = "faint" if hop.dim else "muted"
        text = Text()
        if hop.cursor:
            # There is no chip fill to carry the white of the slot. Thus it takes the
            # reverse-video `selected` grey of the app. This is the same block as the one
            # that draws the committing button of a dialog, and the same block that the
            # cursor of the editor always had.
            text.append(hop.label, style="selected")
        elif hop.dim:
            text.append(hop.label, style="faint")
        elif hop.style:
            text.append(hop.label, style=hop.style)
        elif hop.you:
            text.append(hop.label, style="you")
        elif hop.key and hop.lit_bytes > 0:
            split = hop.lit_bytes * 2  # the two tones of highlighted_hash, in hex digits
            text.append(hop.label[:split], style=node_style(hop.key))
            text.append(hop.label[split:], style="muted")
        elif hop.key:
            text.append(hop.label, style=node_style(hop.key))
        else:
            # There is no key from which to get a hue. The hop is a bare hash for a node
            # that MeshTerm cannot identify. Thus it takes the grey of the app for an
            # unknown node, and not `muted`. `muted` is chrome, and on a console that has
            # only two greys it is a step darker.
            text.append(hop.label, style="node.unknown")
        if hop.annotation:
            text.append(f" ({hop.annotation})", style=note_style)
        return text

    def _render_chips(
        self, hops: list[PathHop], *, carry_in: bool = False, carry_on: bool = False
    ) -> Text:
        """Powerline chips: each hop is filled with its hue, and each seam is one cell.

        The two outer ends tell the user if the line shows the whole route. Two separate
        things can make it shorter. **This line** can be one of several lines into which a
        wrapped path folds (``carry_in`` and ``carry_on``). Also, **the path itself** can be
        only the middle of a route. For example, the ``via`` chain of a packet names its
        relays and neither of the nodes between which it went (``from_origin`` and
        ``to_destination`` on the line). Both draw the same mark at that end, because they
        tell the user the same thing: *the route goes on out there, past what is drawn*. A
        head like that opens on the other half of the break. This is the second cell of the
        seam. Thus a fold looks like the seam that it interrupted, and a chain with only
        relays looks like a route that is caught in the middle of a stride. A tail like that
        closes on the point. This is the same cue "goes on" that arrow mode spells with a
        trailing ``→``.

        An end that really is the route's own end opens or closes **rounded**. Where the
        font has no rounded caps, it is **square**: the chip's own pad is the edge, and
        the function appends nothing. A square end is not a degraded cap. It is the one shape
        that is left that says *stop*. The point is the continuation cue. Thus, when a
        finished path closed on a point, it claimed that a hop was cut off (JP, 2026-09-09).
        Before, only the *opening* squared itself off. Then each route on a terminal that has
        only the core glyphs ended on the mark for "there is more". Now the pair reads
        correctly in both directions. A square end is a promise that the node next to it is
        where the route began or ended. Thus a chain that starts and finishes on relays must
        not have a square end (JP, 2026-09-09).

        An elision (:attr:`PathHop.gap`) interrupts the ribbon and does not join it. The chip
        before it closes on the page, the mark is bare on the page, and the chip after it
        opens on its notch. This is the picture of a route whose middle was left out. A
        filled ``⋯`` chip reads as a node with that name.

        Args:
            hops: The hops of this one line, in order.
            carry_in: This line continues a wrapped path (it does not open one).
            carry_on: The path continues past this line (it does not end here).
        """
        colours = [_colours_of(hop) for hop in hops]
        fills = [colour.fill for colour in colours]
        rounded = powerline_full()
        # A fold and a route that is half drawn are two reasons for the same mark. One of
        # them is enough. An elision already breaks the ribbon on that side, and a mark that
        # MeshTerm cuts from a bare ``⋯`` has no fill into which to cut.
        opens_mid = (carry_in or not self._from_origin) and not hops[0].gap
        text = Text()
        if opens_mid:
            text.append_text(self._notch(fills[0]))  # the other half of the break
        elif rounded and not hops[0].gap:
            text.append(POWERLINE_ROUND_OPEN, style=f"{_FLAT}{fills[0]}")
        for i, hop in enumerate(hops):
            if i:
                if hops[i - 1].gap:
                    text.append_text(self._notch(fills[i]))  # the ribbon continues again
                elif hop.gap:
                    text.append(POWERLINE_SEP, style=f"{_FLAT}{fills[i - 1]}")  # …and stops
                else:
                    text.append_text(self._seam(colours[i - 1], colours[i]))
            if hop.gap:
                text.append(hop.label, style="faint" if hop.dim else "muted")
            else:
                text.append_text(self._chip(hop, colours[i]))
        if hops[-1].gap:
            return text  # the line ended on the page, and no chip is left to close
        if carry_on or not self._to_destination:
            text.append(POWERLINE_SEP, style=f"{_FLAT}{fills[-1]}")  # the point: the route goes on
        elif rounded:
            text.append(POWERLINE_ROUND_CLOSE, style=f"{_FLAT}{fills[-1]}")  # the far end
        return text

    @staticmethod
    def _seam(before: _ChipColours, after: _ChipColours) -> Text:
        """The one cell between two chips: the point of the previous fill, laid on the next.

        This is the classic interlock. The foreground is the chip behind, and the background
        is the chip ahead. Thus the route reads as one ribbon whose segments meet on a
        chevron. It costs a single cell. There are 256 node hues on the wheel, so two
        neighbours almost always differ enough that the join reads as a join (JP,
        2026-08-09). Before, the widget used two cells and left a sliver of page between
        each pair. This was insurance against a collision that the palette makes rare.

        Two fills that the eye cannot tell apart are the exception that the interlock cannot
        draw, because a chevron in its own background is not a chevron. This is not always bad
        luck. A mirrored return leg is a run of hops that are identically faded, by
        construction. A stretch of keyless greys is the same. "Cannot tell apart" is also not
        the same as "identical". Two hues that are a few steps apart on the wheel draw a point
        that the eye cannot find, for example one shade of green on the next. Thus the test is
        a perceptual distance of less than :data:`SEAM_BLUR` (:func:`_fills_blur`). It is not
        equality, and it is not a gap of hue.

        There the seam is the **thin** chevron, :data:`POWERLINE_THIN`. It is drawn on the
        fill ahead, in the colour of the label of the previous chip. It is powerline's
        own mark for a join inside one colour, and the chip to which it belongs draws a line
        on itself. Thus the ribbon continues without a break, and nothing on it is a colour
        that is not a colour of a chip. Before, the solid point that was drawn bare cut a
        wedge of page out of the route here. (A run of near hues read as dashes, and on a
        light terminal the wedge was a hole.) Then the thin chevron was the previous fill,
        one step darker in lightness (JP, 2026-09-16). When the label became the hue of the
        node on a darker fill of itself (2026-10-06), the label colour became the line. It is
        the light half of the same pair, so it is visible on each fill, and it is still the
        own colour of the chip. In both cases the seam is one cell.

        Args:
            before: The colours of the previous chip. The point is drawn in its fill.
            after: The colours of the next chip. The point is laid on its fill.

        Returns:
            The single styled cell of the seam.
        """
        if _fills_blur(before.fill, after.fill):
            return Text(POWERLINE_THIN, style=f"{_FLAT}{before.ink} on {after.fill}")
        return Text(POWERLINE_SEP, style=f"{_FLAT}{before.fill} on {after.fill}")

    @staticmethod
    def _notch(fill: str) -> Text:
        """The left edge of a chip: the point, cut inward out of its own fill.

        Reverse video paints the notch in the terminal's own background. This is the
        only way to name the colour of the page without knowing it. Thus nothing that is on
        the left of the chip (the taper of the previous chip, the line above a wrapped break)
        bleeds into it, and the point still faces the direction in which the path flows.

        Args:
            fill: The fill colour of the chip, out of which the notch is cut.

        Returns:
            The one styled cell.
        """
        text = Text()
        text.append(POWERLINE_SEP, style=f"{_FLAT}{fill} reverse")
        return text

    @staticmethod
    def _chip(hop: PathHop, colours: _ChipColours) -> Text:
        """One chip: the same words as in arrow mode, in the ink of the chip on its fill.

        Each chip has padding on both sides, also the ``★`` (JP, 2026-08-09). The pads are
        not there to make room for a long *word*. They are the chip's own shape. A star
        that is squeezed between two chevrons reads as a glyph that is wedged into the seam,
        and not as a segment of the route. The ink of our chip is the yellow of the map on
        the neutral dark grey :data:`_YOU_BG`. Thus the star in the line and the star on the
        map read as one mark.

        The colours come from the palette of the platform (:func:`_colours_of`).
        """
        fill, ink, soft = colours
        text = Text()
        text.append(" ", style=f"{_FLAT}on {fill}")
        if hop.lit_bytes > 0 and not hop.dim:
            split = hop.lit_bytes * 2
            text.append(hop.label[:split], style=f"{_WEIGHT}{ink} on {fill}")
            text.append(hop.label[split:], style=f"{_FLAT}{soft} on {fill}")
        else:
            weight = _FLAT if hop.dim else _WEIGHT
            text.append(hop.label, style=f"{weight}{ink} on {fill}")
        if hop.annotation:
            text.append(f" ({hop.annotation})", style=f"{_FLAT}{soft} on {fill}")
        text.append(" ", style=f"{_FLAT}on {fill}")
        return text


def _spectrum_colours(hop: PathHop) -> _ChipColours:
    """The colours of a chip on a truecolor terminal: the hue on a darker fill of itself.

    The label of a node is its hue. This is the colour that its name has in each list and
    in arrow mode. The fill is that hue, but darker (:data:`CHIP_LIGHTNESS`). The whole
    label is in the hue, also a hash label and an annotation. The lit bytes of a hash are
    bold. A keyless chip is the same pair in grey. Our ``★`` and a faded hop keep their dark
    greys, which already carry a light label. The PicoCalc draws the same rules at the
    resolution of its console (:func:`_slot_colours`).

    The insertion slot wins over all other rules. It is the one chip that is not a node, and
    it has the ``cursor`` white. Thus it reads as chrome among identities, and not as a hop
    with a hue that is unlucky. (White is outside the node spectrum, and its dark text stays
    readable on it.) It is the only light chip on the ribbon. The fade is next, and it wins
    over the identity, also our own. A dimmed hop is a hop that nobody composed (an
    automatic return home, a mirrored return leg). Our end must recede with the rest of
    that automatic half, and not keep its yellow among the greys. Plain mode says the same
    thing when it fades the name.
    """
    if hop.cursor:
        return _ChipColours(_style_hex("cursor") or _KEYLESS_BG, _CHIP_FG, _CHIP_FG)
    resolved = _style_hex(hop.style) if hop.style else None
    if resolved:
        return _ChipColours(_chip_fill(resolved), resolved, resolved)
    if hop.dim:
        return _ChipColours(_DIM_BG, _DIM_FG, _DIM_FG)
    if hop.you:
        return _ChipColours(_YOU_BG, _SELF_INK, _SELF_INK)
    hue = _style_hex(node_style(hop.key)) if hop.key else None
    if hue:
        return _ChipColours(_chip_fill(hue), hue, hue)
    return _ChipColours(_KEYLESS_BG, _KEYLESS_FG, _KEYLESS_FG)


def _slot_colours(hop: PathHop) -> _ChipColours:
    """The colours of a chip on the 16-colour console: a bright slot on its dim twin.

    The VT draws a background only from slots 0 to 7. These are the dim twins of the bright
    slots that give the colour of the node names. Thus the chip of a node is the dim twin of
    its hue, and its label is in the hue itself. This is the colour that its name has in
    arrow mode and in each list. A pink name gets a purple chip, and a yellow name gets a
    brown chip. The whole label is in this colour, also a hash label. A grey for the rest of
    a hash was not readable on the green and the cyan fills (JP, 2026-10-06).

    A keyless chip is white on light grey, the same pair one bank down. Our ``★``, a faded
    hop, and the slot of the composer have no fill of their own, because the console has no
    dark grey background. They stand on the page (:data:`_SLOT_PAGE`): the star in its
    yellow, a faded hop in the dark grey, and the slot as a white ``+`` in a break of the
    ribbon. The order of the rules is the same as on a truecolor terminal
    (:func:`_spectrum_colours`).
    """
    if hop.cursor:
        return _ChipColours(_SLOT_PAGE, _SLOT_WHITE, _SLOT_WHITE)
    if hop.style:
        resolved = _style_hex(hop.style)
        if resolved:
            fill = DIM_TWIN.get(resolved, resolved)
            ink = resolved if resolved in DIM_TWIN else _SLOT_WHITE
            return _ChipColours(fill, ink, ink)
    if hop.dim:
        return _ChipColours(_SLOT_PAGE, _SLOT_FAINT, _SLOT_FAINT)
    if hop.you:
        return _ChipColours(_SLOT_PAGE, _SELF_INK, _SELF_INK)
    hue = _style_hex(node_style(hop.key)) if hop.key else None
    ink = hue if hue in DIM_TWIN else _SLOT_WHITE
    return _ChipColours(DIM_TWIN[ink], ink, ink)


#: The colours of a chip, and the two prefixes of the chip styles. MeshTerm binds them when
#: the platform switches (:func:`_bind_chips`).
_colours_of: Callable[[PathHop], _ChipColours] = _spectrum_colours
#: The weight of a chip label: ``bold`` on a truecolor terminal.
_WEIGHT = "bold "
#: The prefix of each other chip style: empty on a truecolor terminal.
_FLAT = ""


@on_platform
def _bind_chips(platform: Platform) -> None:
    """Bind the chip palette to the platform (runs now and at each switch).

    On the 16-colour console, bold is brightness. Rich merges the base style of a row into
    each span on it. Thus the ``bold`` of a selected row moves each dim fill on the ribbon
    to its bright twin. For this reason each chip style there says ``not bold``, and a label
    gets no weight. Its bright slot is its emphasis.
    """
    global _colours_of, _WEIGHT, _FLAT
    if platform.truecolor:
        _colours_of, _WEIGHT, _FLAT = _spectrum_colours, "bold ", ""
    else:
        _colours_of, _WEIGHT, _FLAT = _slot_colours, "not bold ", "not bold "


def _shorten(value: str, hash_bytes: int | None) -> str:
    """Bare hex in lower case, truncated to ``hash_bytes`` bytes (whole when it is empty)."""
    raw = value.lower().removeprefix("0x")
    return raw[: hash_bytes * 2] if hash_bytes else raw


def path_line(
    hops: Sequence[str | None],
    resolve: Callable[[str], str | None] = lambda hop: hop,
    *,
    prefix_bytes: int = 0,
    self_name: str | None = None,
    empty: str = "direct",
    show_hash: bool = False,
    hash_bytes: int | None = None,
    device_hash: str | None = None,
    dim_from: int | None = None,
    hash_as_name: bool = False,
    bare_self: bool = False,
    dim_self: bool = True,
    cursor: int | None = None,
    mode: str = "auto",
    from_origin: bool = True,
    to_destination: bool = True,
) -> PathLine:
    """Build a :class:`PathLine` from raw hop hashes, with the vocabulary of ``path_text``.

    This is the migration bridge. Each parameter has the same meaning as for
    :func:`~meshterm.ui.widgets.path_text` (refer to that function for the full rendering
    rules). Thus a call site changes builders and does not change what it says. A named hop
    reads ``Name`` (with ``show_hash`` it is ``Name (3d)``). An unnamed hop is its hash in
    the grey for an unknown node (colour marks an identified node, as in the path graph).
    ``None`` is our device in white, or the lone ``★`` with ``bare_self``. ``dim_from``
    fades a resolved tail. The plain rendering is identical to ``path_text`` in characters
    and in style. What the change gives is the shapes of :class:`PathLine` (ellipsized and
    wrapped) and the powerline mode.

    Args:
        hops: The hops in the order of propagation. They are hex hashes, and ``None`` marks
            our device. (The function skips empty strings. ``dim_from`` counts the hops
            that are rendered.)
        resolve: Maps a hop hash to a friendly name when it is known.
        prefix_bytes: The path-hash width at which the identity hash of an unnamed hop shows
            under ``hash_as_name``. (In all other cases, unnamed hops are fully grey, and no
            prefix is lit.)
        self_name: The name of our node. The function makes a resolved name that matches it
            white. It is also the name of each ``None`` device hop.
        empty: The muted text to show when there are no hops (for example ``"direct"``).
        show_hash: Add the hash in parentheses to the named hops (and to our device, with
            ``device_hash``). This is the form for traces.
        hash_bytes: Truncate the hashes that the function shows or adds to this byte width.
        device_hash: The key of our device. The function adds it to the ``None`` hops when
            ``show_hash`` is on.
        dim_from: Fade the hops at this rendered index and after it (``None`` dims nothing).
        hash_as_name: Show each unnamed hop as its muted identity hash at the
            ``prefix_bytes`` width, with the byte by which it is addressed.
        bare_self: Draw our device (a ``None`` hop) as the bare :data:`SELF_GLYPH`. It is for
            a surface whose route *always* begins and ends on us. The name and the hash on
            both ends tell the user nothing new, and they use the cells that the hops
            between them need.
        dim_self: Fade the bare stars (the default). On a surface where the user *composes*
            the route, to set out from us and to come home to us are fixtures. The user does
            not choose or remove them, the same as for a mirrored return leg. Thus they have
            the same automatic grey, and the colour on the line is for the nodes that the
            user really picks. Pass ``False`` where nobody composes anything (a record of a
            walk that is already made). Then there is no "not yours" to say, and our node
            reads in the ``you`` white of the app, as in other places. A ``dim_from`` that
            reaches a star still fades it in both cases.
        cursor: Insert the insertion slot of an editor (:data:`CURSOR_GLYPH`) *at* this
            index of the rendered hops. This is the position that a chosen hop takes, so
            index ``0`` opens the route and ``len`` closes it. The function counts it over
            the rendered hops, the same as ``dim_from``, and applies it after them. Thus a
            slot never changes what a hop shows or how it fades. ``None`` draws no cursor.
        mode: The mode of :class:`PathLine` (``"auto"``, ``"powerline"``, or ``"plain"``).
        from_origin: The first hop is the origin of the route (refer to :class:`PathLine`).
            Clear it where ``hops`` is a chain of *relays*. The raw ``path`` field that a
            packet carries names neither the node that it came from nor the node that it
            reached.
        to_destination: The last hop is the destination of the route. The same reading
            applies.

    Returns:
        The assembled :class:`PathLine`.
    """
    shown = [h for h in hops if h is None or h]
    built: list[PathHop] = []
    for i, hop in enumerate(shown):
        dim = dim_from is not None and i >= dim_from
        if hop is None:
            if bare_self:
                # Where the user composes the route, both our ends are fixed. A walk leaves
                # us and comes home to us, and nobody chooses or removes either end. Thus
                # the stars fade like each other automatic hop, and the colour is for the
                # nodes that the user really picks. Where nobody composes anything
                # (``dim_self=False``), this does not apply, and our node keeps the ``you``
                # white of the app.
                built.append(PathHop(SELF_GLYPH, you=True, dim=dim_self or dim))
                continue
            note = _shorten(device_hash, hash_bytes) if (show_hash and device_hash) else None
            built.append(
                PathHop(self_name or LOCAL_DEVICE_LABEL, you=True, annotation=note, dim=dim)
            )
            continue
        named = resolve(hop)
        if named and named != hop:
            built.append(
                PathHop(
                    named,
                    key=hop,
                    you=bool(self_name and named == self_name),
                    annotation=_shorten(hop, hash_bytes) if show_hash else None,
                    dim=dim,
                )
            )
            continue
        compact = _shorten(hop, hash_bytes)
        if dim:  # a faded unnamed hop shows its compact hash, as path_text does
            built.append(PathHop(compact, dim=True))
        elif hash_as_name:
            identity = _shorten(hop, prefix_bytes or None)
            note = compact if (show_hash and compact and compact != identity) else None
            built.append(PathHop(identity, annotation=note))
        else:
            # No name resolved: the node is unknown, so its hash has no key. The hop takes
            # the grey of the app for an unknown node (in arrows and in chips), and never a
            # hue that its bytes give. Colour marks an identified node, as in the path graph.
            built.append(PathHop(compact))
    if cursor is not None:
        built.insert(max(0, min(cursor, len(built))), PathHop(CURSOR_GLYPH, cursor=True))
    return PathLine(
        built,
        mode=mode,
        empty=empty,
        from_origin=from_origin,
        to_destination=to_destination,
    )
