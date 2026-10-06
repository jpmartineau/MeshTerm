# SPDX-License-Identifier: Apache-2.0
"""Path lines — the flexible hop-sequence text widget (powerline chips or arrows).

This is the successor to :func:`~meshterm.ui.widgets.path_text` as THE way a hop
sequence renders as text — a trace's walked route, a packet's ``via`` chain, a
message's delivery paths, the composer's preview, a trophy row. Where ``path_text``
is one fixed presentation (arrow-joined, one line, overflow left entirely to the
caller), a :class:`PathLine` separates *what the hops are* from *how the line is
drawn*, so every surface the survey found can eventually route through it:

* **Two separator styles.** ``plain`` joins hops with muted ``→`` arrows — today's
  look, unchanged. ``powerline`` renders each hop as a colour-filled chip, joined by one
  solid triangle U+E0B0 interlocked between them — the chip behind as its foreground, the
  chip ahead as its background — so a route reads as a ribbon whose segments meet on a
  chevron: the oh-my-posh look, at one cell a seam. The label of a node's chip is its
  hash-derived hue (:func:`~meshterm.ui.theme.node_style`), the colour that its name has
  on each surface, and the fill is that hue, darker (:data:`CHIP_LIGHTNESS`). Our own
  node is the map's yellow ``★`` on a neutral dark grey, a faded hop is dark slate, and a
  keyless hop is light grey on grey. (On the 16-colour console of the PicoCalc, the fill
  is the dim twin of the hue. Refer to :func:`_slot_colours`.) Two chips that
  land on the *same* fill — a mirrored return leg, a stretch of keyless greys — or on two
  fills too close to tell apart (under :data:`SEAM_BLUR` apart in OKLab, the perceptual
  distance :mod:`~meshterm.ui.oklab` measures) take the one exception the interlock can't
  draw: the seam is the *thin* chevron instead, in the colour of the previous chip's
  label and drawn on the next, so the ribbon runs on unbroken and the
  join is a line the chip draws on itself (:meth:`PathLine._seam`). Hops elided out of
  the middle break the
  ribbon instead of joining it: the mark sits bare on the page between a closing point and
  the next chip's notch (:func:`elision_hop`), because a filled ``⋯`` chip would read as a
  node by that name. A chip caught by a *cut* — a lane that ran out, a line scrolled past
  its edge — breaks off on a half block in its own fill
  (:func:`cut_mark`), and that crack is what says the segment continues; only chips
  crack, an arrow line still ellipsizes. A line's two *outer* ends read the same way:
  a path that begins at its origin and ends at its destination opens and closes flat (a
  rounded cap where the font has one), while one drawing only the *middle* of a route —
  a packet's ``via`` chain, which names relays and neither of the nodes the packet went
  between — wears the chevron at that end instead, the same mark a wrapped line uses for
  "there is more of this out there" (``from_origin`` / ``to_destination``).
  A row that also carries the app-wide
  opens-further-prompts ``…`` hangs it *outside* the budget
  (:func:`with_action_mark`) — chrome may not cost the route cells. ``auto`` (the default)
  picks powerline exactly when the terminal can draw it
  (:func:`~meshterm.ui.termfont.powerline_enabled` —
  a recommended font, a glyph-capable renderer, or the user's override) and falls
  back to arrows everywhere else, so no terminal ever sees tofu.
* **Three overflow answers**, matching the three patterns the surfaces already use:
  :meth:`PathLine.text` is the full one-liner (for callers that crop it with
  :func:`cut_to` or read it through a sliding window of their own, each cut edge
  marked by :func:`cut_mark`), :meth:`PathLine.ellipsized` fits a width by eliding
  hops behind a ``⋯`` mark, on whichever side it is told to eat into
  (:data:`ELIDE_TAIL` by default — both endpoints survive, unlike a plain tail
  truncation that amputates the destination; :data:`ELIDE_HEAD` where a surface
  wants the head given up whole) — and :meth:`PathLine.wrapped` breaks
  at hop boundaries under a hanging indent (never mid-name, never mid-chip),
  folding where the route *means* something: the fewest lines it can take, evened
  out across them rather than greedily crammed, and preferring the seam where the
  path fades from composed to mirrored when that fold is free. A caller that doesn't
  know its width — a value dropped into a label/value grid — hands the
  :class:`PathLine` itself to Rich and gets the wrapped shape at whatever cell width
  the layout works out (see :meth:`PathLine.__rich_console__`).
* **The same hop semantics everywhere.** A :class:`PathHop` carries what the app's
  conventions need: the label (a name, or a hash standing as identity), the key any
  known prefix of which picks the hue, the ``you`` flag, the trace-flavour
  ``(3d)`` annotation, the lit-prefix width for hash labels (the
  ``highlighted_hash`` two-tone), the ``dim`` fade for resolved return legs, and an
  explicit style override for context colourings that outrank identity. Chips keep
  the exact same words as arrows — only colours and separators change — so a route
  reads identically whichever mode drew it. A surface whose route always starts and
  ends on us can ask for ``bare_self``, replacing our name and hash with the app-wide
  ``★`` — the reader already knows who both ends are, and the cells belong to the hops
  that *are* news. Those stars fade where the route is being *composed* (our two ends
  are fixtures, not choices); a surface that only *shows* a walk passes ``dim_self=False``
  and keeps us in the ``you`` white.

Existing call sites still render through ``path_text``; migrating them here is a
separate, per-surface pass. The composer's insertion cursor rides along as a hop in
its own right (``cursor=True``, drawn as the :data:`CURSOR_GLYPH` slot): an insertion
point *is* a position in the route, not a gap between two of them, so it measures,
wraps and colours like every other hop — the preview keeps whichever mode the terminal
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

#: The powerline solid right-pointing triangle (U+E0B0) — the glyph the widget draws
#: at every seam and closing edge, deliberately from the *core* set so every recommended
#: font qualifies (the rounded caps at U+E0B4+ exist only in full Nerd Font patches). A
#: path only ever flows one way, so the point only ever faces right; what changes is
#: whether it lands on a field (a seam) or on the page (an edge).
POWERLINE_SEP = "\ue0b0"

#: The thin right-pointing chevron (U+E0B1), the solid point's outline sibling in the
#: same core set — powerline's own mark for a join *inside* one colour. It draws the seam
#: between two chips whose fills the eye can't tell apart (:meth:`PathLine._seam`): the
#: previous fill, shaded, drawn on the next, where a solid point would have vanished.
POWERLINE_THIN = "\ue0b1"

#: The rounded caps (U+E0B6 opening, U+E0B4 closing) that finish a path's two outer
#: ends as a lozenge. These live in the *extended* block, which only a full Nerd Font
#: patch carries (:func:`~meshterm.ui.termfont.powerline_full`) — a terminal with just
#: the core four squares those ends off instead of showing tofu.
POWERLINE_ROUND_OPEN = ""
POWERLINE_ROUND_CLOSE = ""

#: Our own node, wherever a surface asks for it stripped of name and hash
#: (``bare_self``): the app-wide ``★`` — literally the map's own marker, taken from
#: :data:`~meshterm.ui.marks.SELF_MARK` so the star in a route and the star on the map
#: can never drift apart, in glyph or in hue.
SELF_GLYPH, _SELF_INK = SELF_MARK

#: The composer's insertion cursor, standing in the route as a hop of its own: the
#: empty slot the next chosen hop drops into. A ``+`` because that is exactly what
#: Enter does there — and because no node is ever named one, so the slot can't be
#: misread as a hop that is already in the route.
CURSOR_GLYPH = "+"

#: The plain-mode joining arrow, exactly as ``path_text`` draws it today.
_ARROW = " → "

#: The theme name a rendered path line wears as its base style: it draws *nothing* and
#: exists to be recognised. Every shape this widget mints is stamped with it (see
#: :meth:`PathLine._render`), so a row that appends a path carries a span saying where the
#: route lies — which is how the cursor row's identity fold knows to spare the hop hues
#: (:func:`~meshterm.ui.tui.render._whiten_identities`) instead of whitening a whole route.
PATH_INK = "pathline"

#: The lightness of a node's chip, as a part of the lightness of its hue (OKLab ``L``).
#: On a truecolor terminal, the fill of a node's chip is its hue with ``L`` multiplied by
#: this value, and the label on it is the hue itself: the colour that the name has in each
#: list and in arrow mode. Thus a node is one colour on each surface, and a chip only puts
#: a darker ground of that colour under it. JP chose this look on the PicoCalc
#: (2026-10-06), where the dim twin of a slot is the same rule at the resolution of the
#: console (:data:`~meshterm.ui.theme.DIM_TWIN`). Its dim twins have 0.55 to 0.73 of the
#: lightness of their bright slots.
#:
#: The value came from two ladders of rules over twelve hues. A fixed step down in ``L``
#: made the blue fills almost black, because the blue hue is the darkest on the wheel.
#: One ``L`` for all fills made the blue label hard to read on its fill. ``0.65`` made the
#: blue label weak, and ``0.55`` made the yellow fills dull. The fill also keeps its hue
#: when the darker colour leaves the sRGB gamut: the chroma comes down, refer to
#: :func:`~meshterm.ui.oklab.fitted`. A clip there made the greens near ``0x50`` one colour.
CHIP_LIGHTNESS = 0.60


def _chip_fill(colour: str) -> str:
    """The fill of a chip whose label is ``colour``: the same hue, darker."""
    lightness, a, b = oklab.from_hex(colour)
    return oklab.to_hex(oklab.fitted((lightness * CHIP_LIGHTNESS, a, b)))


def _chip_label(fill: str) -> str:
    """The label for a chip whose fill is ``fill``: the reverse of :func:`_chip_fill`."""
    lightness, a, b = oklab.from_hex(fill)
    return oklab.to_hex(oklab.fitted((min(1.0, lightness / CHIP_LIGHTNESS), a, b)))


#: The text of the composer's slot: near-black slate on the white chip.
_CHIP_FG = "#0f172a"
#: Chip fill for a keyless hop — the ``faint`` grey; colour stays reserved for
#: keyed identities in chips just as it is in arrow mode.
_KEYLESS_BG = "#64748b"
#: The label of a keyless chip: the same pair as a node's chip, in grey.
_KEYLESS_FG = _chip_label(_KEYLESS_BG)
#: Our own node's chip fill: a neutral dark grey, so the yellow ``★`` riding it reads as
#: the map's marker rather than as a node's hue (JP, 2026-08-09). Deliberately *not* a
#: spectrum colour and not the white it used to be — our end of a route is a fixture the
#: reader already knows, so it sits back and lets the hops that differ carry the colour.
_YOU_BG = "#3f3f46"
#: A dimmed hop's chip: dark slate fill with muted ink, receding like ``faint`` text.
_DIM_BG = "#334155"
_DIM_FG = "#94a3b8"

#: On the 16-colour console, the page: slot 0, black. It is the fill of a chip that the
#: console cannot fill, because the VT has no dark grey background. That chip is our
#: ``★``, a faded hop, or the composer's slot. Its text then sits on the page, and the
#: chips on each side close on a point and open on a notch.
_SLOT_PAGE = slot_hex(0)
#: On the console, the text of a faded hop: slot 8, the dark grey.
_SLOT_FAINT = slot_hex(8)
#: On the console, white: slot 15. It is the text of a keyless chip and of the
#: composer's slot.
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


#: The mark standing in for elided hops (see :meth:`PathLine.ellipsized`) — rendered as
#: a dim pseudo-hop so it recedes in both modes.
_ELISION = "⋯"

#: The classic truncation mark, and what an *arrow* line still cuts with: three dots
#: standing where words were. An arrow hop is text, so shortening it is what happened.
_ELLIPSIS = "…"

#: The app-wide "this row opens further prompts" mark (see the UX standards), as a *path*
#: row wears it — see :func:`with_action_mark` for why it is spelled here rather than
#: appended by each surface.
ACTION_MARK = " …"

#: What a *chip* line cuts with instead (JP, 2026-08-09) — the half block, drawn in the
#: cut chip's own fill with no background, so the cell is half segment and half bare
#: page: the chip breaks off mid-body and the crack says it is incomplete. An ellipsis
#: would claim the opposite, that a *word* was shortened, when what ran out is the lane.
#: :data:`CRACK_TAIL` closes a line the path runs on past (the fill's left half survives,
#: the page takes the right); :data:`CRACK_HEAD` is its mirror, opening a line whose
#: start is off-screen. Only chips crack — :func:`cut_mark` finds a fill or falls back to
#: :data:`_ELLIPSIS`, so arrow lines are untouched by the whole idea.
CRACK_TAIL = "▐"
CRACK_HEAD = "▌"

#: Which side of a path an :meth:`PathLine.ellipsized` fit eats into. ``ELIDE_TAIL`` is
#: the route reading — the origin anchors the line, the destination is rescued off the
#: far end, and the mark lands in the middle. ``ELIDE_HEAD`` is the trail reading — the
#: last hop anchors the line and everything before it goes, origin included.
ELIDE_TAIL = "tail"
ELIDE_HEAD = "head"

#: Extra columns a wrapped line steps in past the hanging indent. The continuation
#: marks (a trailing ``→``, a notched chip edge) say the path goes on; the step says
#: it at a glance, from the shape of the block alone — a route that folds reads as one
#: value that ran long, not as a second value stacked under the first.
WRAP_OFFSET = 2


@dataclass(frozen=True)
class PathHop:
    """One hop of a path line — the semantics, independent of how it is drawn.

    Attributes:
        label: What is shown — a resolved name, a hash standing as the node's
            identity (set ``lit_bytes`` for the two-tone prefix in that case), or the
            bare :data:`SELF_GLYPH` where a surface has no need to name us.
        key: Any known prefix of the node's key/hash — picks the hash-derived hue
            (its first byte, so every prefix agrees). ``None`` renders muted/grey:
            colour is reserved for keyed identities.
        you: Our own node. Arrows draw it in the pure-white ``you`` style; chips draw it
            in the map's own yellow on the neutral dark grey :data:`_YOU_BG`, padded like
            every other chip.
        annotation: The trace-flavour hash note, rendered ``" (3d)"`` after the
            label in both modes (pass the bare ``3d``, no parentheses).
        lit_bytes: For a hash label: leading *bytes* drawn in the hue (the
            ``highlighted_hash`` two-tone), the rest muted/soft. ``0`` for names.
        dim: Fade the hop (and the arrow leading into it) — a planned route's
            resolved return leg, "not yours to compose".
        style: Explicit colour override — a hex style (``"#rrggbb"``, attributes
            allowed) or theme name — for context colourings that outrank identity.
            Plain mode uses it as the label style; powerline as the chip fill.
        cursor: This hop is an editor's insertion slot rather than a node — the
            :data:`CURSOR_GLYPH` in the app's ``cursor`` white, the same colour the list
            cursor (``❯``) below it wears, so the two halves of one gesture (the row
            you pick, the slot it lands in) read as one thing.
        gap: This hop is a mark standing *between* two nodes rather than being one — the
            elision (:func:`elision_hop`). Chips draw it in the break between two
            segments, bare on the page with no fill and no padding, because what it says
            is "the ribbon stops here and picks up again"; a chip would say the opposite,
            that some node in the route is called ``⋯``. Arrow mode needs no special case:
            a bare label between two arrows is already exactly that.
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
    """The mark standing in for hops dropped out of a path's middle (or its head).

    THE way to say "hops were left out here", so every surface that shortens a path
    agrees on the glyph *and* on the fact that it is a gap rather than a node (see
    :attr:`PathHop.gap`).
    """
    return PathHop(_ELISION, dim=True, gap=True)


def _style_hex(style: str) -> str | None:
    """The ``#rrggbb`` a style string or theme name resolves to, else ``None``."""
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


#: How far apart two chip fills must sit, as the eye measures it, for the seam between
#: them to interlock: closer than this and the solid point would be laid on a field it
#: can't be told from, so the seam is the thin chevron instead (:meth:`PathLine._seam`).
#: The unit is OKLab distance (:func:`~meshterm.ui.oklab.distance`), where ``0.02`` is a
#: just-noticeable difference between two large patches and ``1`` is black to white.
#:
#: The rule it replaced was first key bytes under ``0x10`` apart on the hue wheel (JP,
#: 2026-09-15), which measures ``0.087`` at the median hue but ``0.026`` across the greens
#: and ``0.165`` across the cyans: the wheel is not uniform to the eye, so a hue gap
#: blurred too little there and too much here, and an sRGB distance would only have
#: restated the hue gap (a fixed byte step is a near-constant ``55``–``60`` of 8-bit RGB
#: all the way round). The value was then set by eye on a ladder of the spectrum's own
#: pairs drawn both ways (JP, 2026-09-16), and it sits where the solid point is not just
#: detectable at one cell but *found* without looking for it: ``0.06``, three noticeable
#: differences. Tighter (``0.035``) left wedges a reader had to hunt for; looser
#: (``0.10``) blurred pairs whose point still read plainly; ``0.05`` was a shade too
#: tight. ``0x10`` of green is ``0.026`` and blurs; ``0x10`` of red is ``0.124`` and
#: doesn't; the two greys, ``0.18`` apart, keep their interlock on the metric alone.
SEAM_BLUR = 0.06


def _fills_blur(before: str, after: str) -> bool:
    """Whether two fills are the same to the eye: identical, or under :data:`SEAM_BLUR`."""
    return before == after or oklab.distance(before, after) < SEAM_BLUR


#: A chip's fill, as it appears in a rendered span's style — the ``on #rrggbb`` half.
#: Only :meth:`PathLine._chip` sets a background, which is what makes this the test for
#: "is there a chip here": a seam's point, a notch, and every arrow-mode span carry a
#: foreground alone.
_ON_FILL = re.compile(r"\bon\s+(#[0-9a-fA-F]{6})\b")


def _fills(line: Text) -> list[str | None]:
    """The chip fill under each *character* of a rendered line (``None`` off a chip)."""
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
    """The character index a display cell falls in, clamped to the string's ends."""
    if cell <= 0:
        return 0
    at = 0
    for i, ch in enumerate(plain):
        at += cell_len(ch)
        if at > cell:
            return i
    return max(0, len(plain) - 1)


def cut_mark(line: Text, at: int, side: str) -> Text:
    """The one cell that says a rendered path line was cut here.

    A chip caught by the cut breaks off in its own colour (:data:`CRACK_TAIL` /
    :data:`CRACK_HEAD`) — half the cell is still segment, half is bare page, so the
    reader sees a chip that *continues* rather than a route that ended. A line drawn in
    arrows has no fill to shear, so it cuts the classic way, with :data:`_ELLIPSIS`; that
    fallback is the whole mode test, and it costs the caller nothing to ask for.

    The fill is read from the nearest cell *inside* the line — scanning back from ``at``
    for a tail cut, forward for a head cut — so a cut landing on a seam or on the closing
    edge still cracks in the colour of the chip the reader can see, not in nothing.

    Args:
        line: The rendered line being cut (whole, unshifted — cells are absolute).
        at: The cell index of the nearest *visible* cell on the mark's side: the last
            one still drawn for :data:`ELIDE_TAIL`, the first for :data:`ELIDE_HEAD`.
        side: Which end of the line the mark closes — :data:`ELIDE_TAIL` (the path runs
            on to the right) or :data:`ELIDE_HEAD` (it began off to the left).

    Returns:
        The styled single cell, ready to append or prepend.
    """
    fills = _fills(line)
    if not fills:  # nothing drawn to shear — a caller cutting an empty line
        return Text(_ELLIPSIS, style="muted")
    index = _char_of_cell(line.plain, at)
    steps = range(index, len(fills)) if side == ELIDE_HEAD else range(index, -1, -1)
    fill = next((fills[i] for i in steps if fills[i]), None)
    if fill is None:
        return Text(_ELLIPSIS, style="muted")
    return Text(CRACK_HEAD if side == ELIDE_HEAD else CRACK_TAIL, style=f"{_FLAT}{fill}")


def with_action_mark(line: Text, width: int) -> Text:
    """``line`` plus the opens-further-prompts ``…`` — riding *past* the width budget.

    The mark says what Enter on the row does. It is not part of the path, so it must not
    cost the path a cell: reserving room for it up front made a route that fitted its lane
    exactly crack on its last chip (JP, 2026-08-10), so the row claimed the walk ran on
    when the walk had in fact finished — the crack only replaces the closing cap, and the
    two cells the mark wanted were the two the reader lost.

    So the path is fitted to the *whole* lane first and the mark lands in whatever is left
    over; where nothing is left, it simply isn't drawn. Losing it costs the reader a hint
    the row's own behaviour gives them the moment they press Enter; losing the tail of a
    hop costs them the route. That is the widget's default answer wherever a path row
    carries the mark, not one surface's local trade — pass the *fitted* line here (after
    :func:`cut_to` or after a scroll window) and let the leftovers decide.

    Args:
        line: The path line, already fitted to ``width``.
        width: The lane's cell budget.

    Returns:
        The line with the mark appended, or unchanged when the lane has no room for it.
    """
    if line.cell_len + cell_len(ACTION_MARK) > width:
        return line
    out = line.copy()
    out.append(ACTION_MARK, style="muted")
    return out


def cut_to(line: Text, width: int, *, action: bool = False) -> Text:
    """Fit a rendered path line to ``width`` by cutting its tail — cracked, not elided.

    The drop-in for ``Text.truncate(width, overflow="ellipsis")`` wherever the line being
    cut is a path: the body is cropped a cell short and the freed cell carries
    :func:`cut_mark`, so chips break off in their own colour and arrow lines end in the
    same ``…`` they always did. A line that already fits comes back untouched.

    Args:
        line: The rendered line to fit.
        width: The cell budget.
        action: Append the opens-further-prompts mark afterwards, free of the budget
            (:func:`with_action_mark`) — for a row Enter opens something from.

    Returns:
        A line no wider than ``width``.
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
    """The hop count, as the stats line hanging under a path line spells it.

    Wherever a surface hangs stats under a path, the number of hops is one of them (JP,
    2026-08-10): it is the figure the picture above encodes but never states, the one two
    routes are most often compared on, and the one a reader would otherwise have to count
    chips to get. ``direct`` where there are none — a path with no relays isn't "0 hops",
    it is the app's own word for a frame that went straight there (:class:`PathLine`'s
    own ``empty`` note says exactly that).

    Args:
        count: How many *relay* hops the route took (endpoints excluded — us and the far
            node are on every route here, so counting them would say nothing).

    Returns:
        The muted atom, ready to join a ``·`` chain.
    """
    if count <= 0:
        return Text("direct", style="muted")
    return Text(f"{count} hop" if count == 1 else f"{count} hops", style="muted")


class PathLine:
    """A hop sequence, renderable as arrow-joined text or powerline chips.

    Build one from :class:`PathHop` entries, then ask for the shape the surface
    needs: :meth:`text` (full line), :meth:`ellipsized` (middle-elided to a width),
    or :meth:`wrapped` (hop-boundary lines under a hanging indent).

    Args:
        hops: The hops in display order (the widget never reorders; a reversed or
            mirrored walk is the caller's composition).
        mode: ``"auto"`` (powerline when the terminal can draw it — the default),
            ``"powerline"``, or ``"plain"``.
        separator: The plain-mode joiner. Defaults to the app-wide ``" → "``; a
            surface with its own convention (the mesh walk trail's ``" › "``) may pass
            it. Ignored by powerline mode.
        empty: The muted text a hopless path reads as (``"direct"``).
        from_origin: The first hop *is* the route's origin. Clear it where the line
            draws only the middle of a route — a packet's ``via`` chain names its
            relays and neither the node that sent it nor the one that received it — and
            the head is drawn as a continuation instead of as a beginning.
        to_destination: The last hop *is* the route's destination. Clear it under the
            same reading at the other end.
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
        """Hold the hops and the drawing choices; nothing is measured or drawn yet.

        Every argument is described in the class docstring above.
        """
        self._hops = list(hops)
        self._mode = mode
        self._separator = separator
        self._empty = empty
        self._from_origin = from_origin
        self._to_destination = to_destination

    @property
    def hops(self) -> list[PathHop]:
        """The hops, for callers that splice lines together (endpoints + relays)."""
        return list(self._hops)

    # --- the three shapes ---------------------------------------------------------

    def text(self) -> Text:
        """The full one-line rendering; overflow is the caller's call, per surface.

        Returns:
            A one-line :class:`Text` (the ``empty`` note when there are no hops).
        """
        if not self._hops:
            return Text(self._empty, style="muted")
        return self._render(self._hops)

    def ellipsized(self, width: int, *, elide: str = ELIDE_TAIL) -> Text:
        """The line fitted to ``width`` by eliding hops behind a ``⋯`` mark.

        ``elide`` names the *side* the mark eats into — which end of the path pays for
        the overflow:

        * :data:`ELIDE_TAIL` (the default) anchors the line on its head: the origin
          holds and hops go from the tail end. The destination is rescued out of that
          side along with as much of the run before it as fits, so the ``⋯`` settles in
          the *middle* and both endpoints survive — a route reads origin and
          destination first, and a plain right-truncation would amputate the second.
          The origin is only given up if even that won't fit, and a lone over-long hop
          is cut where the width runs out as the last resort (:func:`cut_to` — a chip
          cracks off in its own colour, an arrow line ellipsizes).
        * :data:`ELIDE_HEAD` anchors the line on its tail: the last hop holds and hops
          go from the head end, the origin among them, with no endpoint rescued. A
          breadcrumb trail's news is where the walk *is*; where it set out from is
          exactly the part worth losing.

        Args:
            width: The cell budget the returned line must fit in.
            elide: Which side the ``⋯`` eats into — :data:`ELIDE_TAIL` (the default,
                keeping both endpoints) or :data:`ELIDE_HEAD` (keeping the tail alone).

        Returns:
            A one-line :class:`Text` no wider than ``width``.
        """
        full = self.text()
        if full.cell_len <= width or not self._hops:
            return full
        mark = elision_hop()
        count = len(self._hops)
        # The heads worth trying, most-rescued first: a tail-side elision spares the
        # origin while it fits and gives it up only when it must; a head-side one is
        # eating the head on purpose, so it never spares it at all.
        for head in (0,) if elide == ELIDE_HEAD else (1, 0):
            for tail in range(count - 1 - head, 0, -1):
                kept = self._hops[:head] + [mark] + self._hops[-tail:]
                candidate = self._render(kept)
                if candidate.cell_len <= width:
                    return candidate
        last = self._render([mark, self._hops[-1]]) if count > 1 else full
        return cut_to(last, width)

    def wrapped(self, width: int, *, indent: int = 0) -> list[Text]:
        """The line broken at hop boundaries, hanging under ``indent`` columns.

        The hanging-indent convention: the caller lays its label lane on the first
        line, so that line is returned *without* the indent prefix while every
        continuation starts with ``indent`` spaces — plus :data:`WRAP_OFFSET`, the
        step that makes a fold legible as a fold. Every line fits ``width``.
        Plain-mode lines that
        continue end with the separator's own mark — a trailing ``→`` for an
        arrow-joined path, the bare ``,`` for a comma-joined wire spec — the "path
        goes on" cue; chip lines always close with their pointed edge and, from the
        second line down, open by redrawing the seam the break interrupted — the point
        arriving out of the previous line's last fill — so a continuation is never
        mistakable for a path starting over. A single hop wider than the content column
        stands alone, cut to it by :func:`cut_to`: a chip breaks off on the crack, an
        arrow hop ellipsizes.

        Where the breaks fall is chosen for how it *reads* (see :meth:`_flow` and
        :meth:`_turn_seam`), not by cramming each line full: the fold takes the
        fewest lines the hops allow, spread evenly across them, and lands on the
        route's turn — where a leg gives way to its mirrored return — when folding
        there costs no extra line.

        Args:
            width: The full line budget, indent included.
            indent: The hanging-indent column the continuations align under.

        Returns:
            The lines, in order (a single ``empty`` note when there are no hops).
        """
        if not self._hops:
            return [Text(self._empty, style="muted")]
        budget = max(1, width - indent)
        plain = self._resolved_mode() != "powerline"
        # The continuation cue is the separator's own mark, minus the space the next
        # hop would have sat in: " → " reads " →", a wire spec's "," stays ",".
        cue = self._separator.rstrip() if plain else ""
        groups = self._flow(self._hops, budget, plain, len(cue), 0)
        seam = self._turn_seam()
        if seam is not None and len(groups) > 1:
            # The outbound leg's own last line still continues into the return, so it
            # holds the cue back where the whole path's final line doesn't.
            legs = self._flow(self._hops[:seam], budget, plain, len(cue), len(cue), ends=False)
            legs += self._flow(self._hops[seam:], budget, plain, len(cue), 0, carry_in=True)
            if len(legs) <= len(groups):  # the turn folds for free — take it
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
            # A lone hop too wide for the column is truncated — leaving room for the
            # cue it still has to carry, so even that line stays inside the width. A
            # column too narrow to hold both drops the cue: content wins the cells.
            continues = i < len(groups) - 1 and 0 < cell_len(cue) < budget
            room = budget - step - (cell_len(cue) if continues else 0)
            if body.cell_len > room:
                body = cut_to(body, room)
            line.append_text(body)
            if continues:
                line.append(cue, style="muted")
            lines.append(line)
        return lines

    # --- the shape a Rich layout asks for -----------------------------------------

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render into a Rich layout — a table cell, a group — as the wrapped shape.

        The one shape a caller doesn't have to size itself. Dropped straight into a
        label/value grid (the packet viewer's ``via`` row), the line folds at hop
        boundaries to whatever cell width the layout works out for the value column,
        hanging its continuations under the value block exactly as :meth:`wrapped`
        lays them out — so a surface whose lane width is decided *by* the other rows
        never has to compute it twice. A caller that already knows its width still
        asks for a shape directly.
        """
        yield from self.wrapped(max(1, options.max_width))

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        """How wide the line wants to be: the whole one-liner, down to one hop's cells.

        The maximum is the unwrapped line — given the room, a route reads best without
        folding at all. The minimum is the widest single hop plus the chrome every line
        carries (a chip's closing edge, a continuation's reopened seam, the
        :data:`WRAP_OFFSET` step): below that no column can hold a hop whole, so a
        narrower lane is the layout choosing to crop rather than to wrap.
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
        """Each hop's own width, plus what joining, opening and closing a line costs.

        Fitting hops to a column is arithmetic once every hop has been measured once —
        which matters because :meth:`_flow` tries a whole series of columns, and the
        screens re-fit on every repaint (a spinner tick included).

        Args:
            hops: The hops to measure, in order.
            plain: Measure as arrow text rather than as chips.

        Returns:
            ``(cells, join, tail, lead, head, foot)`` — each hop's cells, the cells one
            join between two hops costs (chip mode's seam is the single interlocked
            chevron — and so is either side of an elision gap, so the figure holds there
            too), the cells a line the path *runs on past* closes with (chip mode's
            point; nothing in arrow mode), the cells every line *after the first* opens
            with (chip mode's notch, the seam redrawn), and the two outer caps: what the
            *first* line opens with and what the line that *ends* the path closes with —
            one cell each where the font has the rounded caps, nothing where it hasn't,
            a squared end being drawn by appending nothing at all.
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
        # An end the route runs on past costs its chevron whatever the font can draw:
        # the point and the notch are core glyphs, and it is the *cap* that is optional.
        # So a relays-only line is measured a cell wider at that end than a whole route
        # is on a terminal with no rounded caps to spend.
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
        """Pack hop widths into lines of ``budget`` cells, greedily, never splitting one.

        Args:
            cells: Each hop's width, in order (see :meth:`_measure`).
            join: Cells one join between two hops costs.
            tail: Cells a line the path runs on past closes with.
            budget: The content column's width in cells.
            reserve: Cells held back on a line for the continuation cue it will carry.
            last: Cells held back on a line ending at the *final* hop — ``0`` when the
                path really ends there (nothing follows to cue), ``reserve`` when this
                is only one leg of a path that goes on.
            lead: Cells every line but the first opens with — the reopened seam plus
                the :data:`WRAP_OFFSET` step it hangs past the indent.
            head: Cells the first line opens with (the rounded cap, when drawn).
            foot: Cells the line that *ends the path* closes with (the rounded cap, when
                drawn) — charged in place of ``tail``, a finished path closing on a cap
                or, where the font has none, on nothing at all.
            ends: These hops finish the path, so their last line pays ``foot``. ``False``
                where they are one leg of a route that goes on and every line, the last
                included, still closes on the point.

        Returns:
            How many hops each line takes; a hop too wide for ``budget`` gets a line of
            its own (the caller truncates it).
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
        """Break ``hops`` into the fewest lines, then even those lines out.

        Greedy packing alone widows the tail: a route whose last hop misses the first
        line by a cell or two strands ``us`` alone underneath a full one. Since the
        line *count* is what costs screen rows — and greedy already achieves the
        minimum — the same count is re-packed at the narrowest column that still
        needs it, which spreads the hops across the lines instead of cramming the
        first and starving the last (``us → a → b → c →`` / ``us`` becomes
        ``us → a →`` / ``b → c → us``).

        Args:
            hops: The hops to break up, in order.
            budget: The content column's width in cells.
            plain: Render as arrows rather than chips.
            reserve: Cells held back on a line for the continuation cue it carries.
            last: Cells held back on the line the hops end on (see :meth:`_fill`).
            carry_in: These hops are a *later* leg of a path already under way, so
                even their first line opens as a continuation, not as a beginning.
            ends: These hops finish the path, so their last line closes on the outer cap
                rather than on the continuation point (see :meth:`_fill`).

        Returns:
            The hops grouped per line.
        """
        cells, join, tail, lead, head, foot = self._measure(hops, plain)
        lead += WRAP_OFFSET  # every continuation steps in past the hanging indent
        if carry_in:
            head = lead

        def fill(column: int) -> list[int]:
            return self._fill(cells, join, tail, column, reserve, last, lead, head, foot, ends)

        lines = len(fill(budget))
        low, high = 1, budget
        while low < high:  # the narrowest column still taking `lines` rows
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
        """The hop index a fold would read best at — where the route turns — or ``None``.

        Two things mark a turn. A dimmed *tail* is one: the composed outbound leg ends
        and the mirrored return begins (see :attr:`PathHop.dim`). It is read as the run
        of faded hops that reaches the end of the line, not merely the first faded hop —
        a route's opening ``★`` fades too (setting out from us is nobody's choice
        either), and that lone fixture at the head marks nothing. A walked boomerang is
        the other — nothing is dimmed once a trace has actually answered, but the hop
        sequence is its own mirror, so its middle *is* that same turn; folding the plan
        and the walk that answered it in the same place keeps the two readings of one
        route looking like one route. Either way the fold needs real legs on both
        sides — at least two hops each — so a path whose only faded tail is the
        automatic landing back on us never widows it onto a line of its own; that lone
        hop marks no turn, and the walk's own mirror still gets to name one.
        """
        seam: int | None = len(self._hops)
        while seam and self._hops[seam - 1].dim:
            seam -= 1
        if seam == len(self._hops):  # nothing faded at the tail
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
        """The effective mode: ``auto`` resolves against the terminal's verdict."""
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
        """Join ``hops`` in the effective mode, marked as a path line.

        The two carry flags mark a wrapped line's ends and only mean anything to
        chips; arrow mode says the same thing with the trailing cue.

        Every shape leaves through here, so this is where the line stamps its own extent:
        the returned text's base style is :data:`PATH_INK`, which draws nothing and says
        "these cells are a route". A row that appends it (``Text.append_text``) carries
        that stamp along as a span over the hops, and the cursor row's identity fold reads
        it there — see :func:`~meshterm.ui.tui.render._whiten_identities`. Inside a path
        line the hue is not decoration on a name, it is what tells one hop from the next,
        so the highlight lights the row without eating it.
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
        """Arrow-joined hops — ``path_text``'s presentation, hop by hop.

        An end the route runs on past wears the separator with nothing on the far side
        of it: a leading ``→`` where the origin isn't drawn, a trailing one where the
        destination isn't. Arrow mode already spells "the path goes on" with that
        trailing mark when a line wraps, so a chain of relays says it the same way —
        which is what keeps the two modes telling the reader the same thing on a console
        with no chevrons to shear (see :meth:`_render_chips`). The wrap flags win where
        they overlap: a continuation already opens under its predecessor, and
        :meth:`wrapped` appends the cue itself on a line the path outruns.
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
        """One arrow-mode hop: label in its identity style, annotation muted."""
        note_style = "faint" if hop.dim else "muted"
        text = Text()
        if hop.cursor:
            # No chip fill to carry the slot's white, so it takes the app's reverse-video
            # `selected` grey — the same block a dialog's committing button is drawn as,
            # and the same block the editor's cursor has always been.
            text.append(hop.label, style="selected")
        elif hop.dim:
            text.append(hop.label, style="faint")
        elif hop.style:
            text.append(hop.label, style=hop.style)
        elif hop.you:
            text.append(hop.label, style="you")
        elif hop.key and hop.lit_bytes > 0:
            split = hop.lit_bytes * 2  # the highlighted_hash two-tone, in hex digits
            text.append(hop.label[:split], style=node_style(hop.key))
            text.append(hop.label[split:], style="muted")
        elif hop.key:
            text.append(hop.label, style=node_style(hop.key))
        else:
            # No key to derive a hue from: the hop is a bare hash standing in for a node
            # we can't identify, so it takes the app-wide unknown-node grey — not `muted`,
            # which is chrome and sits a step darker on a console with only two greys.
            text.append(hop.label, style="node.unknown")
        if hop.annotation:
            text.append(f" ({hop.annotation})", style=note_style)
        return text

    def _render_chips(
        self, hops: list[PathHop], *, carry_in: bool = False, carry_on: bool = False
    ) -> Text:
        """Powerline chips: each hop filled with its hue, a two-cell gap at every seam.

        The two outer ends say whether the reader is looking at the whole route, and
        two separate things can shorten it. **This line** may be one of several a wrapped
        path folds into (``carry_in``/``carry_on``), and **the path itself** may only ever
        have been the middle of a route — a packet's ``via`` chain names its relays and
        neither of the nodes it actually went between (``from_origin``/``to_destination``
        on the line). Either one draws the same mark at that end, because the reader is
        being told the same thing: *the route goes on out there, past what is drawn*. A
        head like that opens on the break's other half — the seam's own second cell, so a
        fold looks like the seam it interrupted, and a relays-only chain looks like a
        route caught mid-stride. A tail like that closes on the point, the same "goes on"
        cue arrow mode spells with a trailing ``→``.

        An end that really is the route's own opens or closes **rounded**, and, where the
        font has no rounded caps, **square** — the chip's own pad is the edge, and nothing
        is appended. Square is not a degraded cap, it is the one shape left that says
        *stop*: the point is spoken for as the continuation cue, so closing a finished
        path on one claimed a hop had been cut off (JP, 2026-09-09). Only the *opening*
        used to square itself off, which left every route on a core-only terminal ending
        on the mark for "there is more". The pair now reads both ways round — a square end
        is a promise that the node beside it is where the route began or ended, which is
        why a chain that starts and finishes on relays must not wear one (JP, 2026-09-09).

        An elision (:attr:`PathHop.gap`) interrupts the ribbon rather than joining it: the
        chip before it closes on the page, the mark sits on the page bare, and the chip
        after it opens on its notch — the picture of a route whose middle was left out,
        where a filled ``⋯`` chip would have read as a node by that name.

        Args:
            hops: The hops of this one line, in order.
            carry_in: This line continues a wrapped path (it does not open one).
            carry_on: The path continues past this line (it does not end here).
        """
        colours = [_colours_of(hop) for hop in hops]
        fills = [colour.fill for colour in colours]
        rounded = powerline_full()
        # A fold and a half-drawn route are two reasons for the same mark; either is
        # enough. An elision already breaks the ribbon on that side, and a mark cut out
        # of a bare ``⋯`` would have no fill to cut into.
        opens_mid = (carry_in or not self._from_origin) and not hops[0].gap
        text = Text()
        if opens_mid:
            text.append_text(self._notch(fills[0]))  # the break's other half
        elif rounded and not hops[0].gap:
            text.append(POWERLINE_ROUND_OPEN, style=f"{_FLAT}{fills[0]}")
        for i, hop in enumerate(hops):
            if i:
                if hops[i - 1].gap:
                    text.append_text(self._notch(fills[i]))  # the ribbon picks up again
                elif hop.gap:
                    text.append(POWERLINE_SEP, style=f"{_FLAT}{fills[i - 1]}")  # …and stops
                else:
                    text.append_text(self._seam(colours[i - 1], colours[i]))
            if hop.gap:
                text.append(hop.label, style="faint" if hop.dim else "muted")
            else:
                text.append_text(self._chip(hop, colours[i]))
        if hops[-1].gap:
            return text  # the line ended on the page; there is no chip left to close
        if carry_on or not self._to_destination:
            text.append(POWERLINE_SEP, style=f"{_FLAT}{fills[-1]}")  # the point: it goes on
        elif rounded:
            text.append(POWERLINE_ROUND_CLOSE, style=f"{_FLAT}{fills[-1]}")  # the far end
        return text

    @staticmethod
    def _seam(before: _ChipColours, after: _ChipColours) -> Text:
        """The one cell between two chips: the previous fill's point, laid on the next.

        The classic interlock — foreground the chip behind, background the chip ahead —
        so the route reads as one ribbon whose segments meet on a chevron. It costs a
        single cell, and with 256 node hues on the wheel two neighbours are near enough
        to always differ that the join reads as a join (JP, 2026-08-09; the widget used
        to spend two cells leaving a sliver of page between every pair, insurance against
        a collision that the palette makes rare).

        Two fills the eye can't tell apart are the exception the interlock cannot draw —
        a chevron in its own background is no chevron — and they are not always bad luck:
        a mirrored return leg is a run of identically faded hops by construction, and so
        is a stretch of keyless greys. Nor is "can't tell apart" the same as "identical":
        two hues a few steps apart on the wheel draw a point the eye can't find either,
        one shade of green on the next, so the test is a perceptual distance under
        :data:`SEAM_BLUR` (:func:`_fills_blur`), not equality and not a hue gap.

        There the seam is the **thin** chevron, :data:`POWERLINE_THIN`, drawn on the fill
        ahead in the colour of the previous chip's label: powerline's own mark for a join
        inside one colour, and the chip it belongs to drawing a line on itself, so the
        ribbon runs on unbroken and nothing on it is a colour that isn't a chip's. The
        solid point drawn bare used to cut a wedge of page out of the route here (a run of
        near hues read as dashes, and on a light terminal the wedge was a hole). The thin
        chevron was then the previous fill, a lightness step darker (JP, 2026-09-16). When
        the label became the node's hue on a darker fill of itself (2026-10-06), the label
        colour became the line: it is the light half of the same pair, so it is visible
        on each fill and is still the chip's own colour. One cell either way.

        Args:
            before: The colours of the previous chip. The point is drawn in its fill.
            after: The colours of the next chip. The point is laid on its fill.

        Returns:
            The seam's single styled cell.
        """
        if _fills_blur(before.fill, after.fill):
            return Text(POWERLINE_THIN, style=f"{_FLAT}{before.ink} on {after.fill}")
        return Text(POWERLINE_SEP, style=f"{_FLAT}{before.fill} on {after.fill}")

    @staticmethod
    def _notch(fill: str) -> Text:
        """A chip's left edge: the point cut inward out of its own fill.

        Reverse video paints the notch in the terminal's own background — the only way
        to name the page's colour without knowing it — so nothing of whatever sits left
        of the chip (the previous chip's taper, the line above a wrapped break) bleeds
        into it, and the point still faces the way the path flows.

        Args:
            fill: The chip's fill colour, which the notch is cut out of.

        Returns:
            The one styled cell.
        """
        text = Text()
        text.append(POWERLINE_SEP, style=f"{_FLAT}{fill} reverse")
        return text

    @staticmethod
    def _chip(hop: PathHop, colours: _ChipColours) -> Text:
        """One chip: the same words as arrow mode, in the chip's ink on its fill.

        Every chip is padded on both sides, the ``★`` included (JP, 2026-08-09): the pads
        are not there to make room for a long *word*, they are the chip's own shape, and a
        star squeezed between two chevrons reads as a glyph wedged into the seam rather
        than as a segment of the route. Our own chip's ink is the map's yellow on the
        neutral dark grey :data:`_YOU_BG`, so the star in the line and the star on the map
        read as one mark.

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

    A node's label is its hue: the colour that its name has in each list and in arrow
    mode. The fill is that hue, darker (:data:`CHIP_LIGHTNESS`). The whole
    label is in the hue, a hash label and an annotation too, and the lit bytes of a hash
    are bold. A keyless chip is the same pair in grey. Our ``★`` and a faded hop keep their
    dark greys, which already carry a light label. The PicoCalc draws the same rules at
    the resolution of its console (:func:`_slot_colours`).

    The insertion slot outranks everything — it is the one chip that isn't a node,
    and it wears the ``cursor`` white so it reads as chrome among identities rather
    than as a hop with an unlucky hue (white sits outside the node spectrum, and its
    dark text stays readable on it). It is the only light chip on the ribbon. The fade
    comes next, outranking identity including our own: a dimmed hop is one nobody
    composed (an automatic landing back home, a mirrored return leg), and our end must
    recede with the rest of that automatic half rather than keep its yellow among the
    greys. Plain mode says the same thing by fading the name.
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

    The VT draws a background only from slots 0 to 7, which are the dim twins of the
    bright slots that colour the node names. Thus a node's chip is the dim twin of its
    hue, and its label is in the hue itself: the colour that its name has in arrow mode
    and in each list. A pink name gets a purple chip, a yellow name a brown chip. The
    whole label is in this colour, a hash label too: a grey for the rest of a hash was
    not readable on the green and the cyan fills (JP, 2026-10-06).

    A keyless chip is white on light grey, the same pair one bank down. Our ``★``, a
    faded hop, and the composer's slot have no fill of their own, because the console
    has no dark grey background. They stand on the page (:data:`_SLOT_PAGE`): the star
    in its yellow, a faded hop in the dark grey, the slot as a white ``+`` in a break of
    the ribbon. The order of the rules is the same as on a truecolor terminal
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


#: The colours of a chip, and the two prefixes of the chip styles. They are bound at
#: platform-switch time (:func:`_bind_chips`).
_colours_of: Callable[[PathHop], _ChipColours] = _spectrum_colours
#: The weight of a chip label: ``bold`` on a truecolor terminal.
_WEIGHT = "bold "
#: The prefix of each other chip style: empty on a truecolor terminal.
_FLAT = ""


@on_platform
def _bind_chips(platform: Platform) -> None:
    """Bind the chip palette to the platform (runs now and on every switch).

    On the 16-colour console, bold is brightness. Rich merges the base style of a row
    into each span on it, so the ``bold`` of a selected row would move each dim fill on
    the ribbon to its bright twin. Thus each chip style there says ``not bold``, and a
    label gets no weight: its bright slot is its emphasis.
    """
    global _colours_of, _WEIGHT, _FLAT
    if platform.truecolor:
        _colours_of, _WEIGHT, _FLAT = _spectrum_colours, "bold ", ""
    else:
        _colours_of, _WEIGHT, _FLAT = _slot_colours, "not bold ", "not bold "


def _shorten(value: str, hash_bytes: int | None) -> str:
    """Bare lowercase hex, truncated to ``hash_bytes`` bytes (whole when falsy)."""
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
    """Build a :class:`PathLine` from raw hop hashes — ``path_text``'s vocabulary.

    The migration bridge: every parameter means exactly what it means to
    :func:`~meshterm.ui.widgets.path_text` (see there for the full rendering rules),
    so a call site swaps builders without changing what it says — a named hop reads
    ``Name`` (annotated ``Name (3d)`` under ``show_hash``), an unnamed hop its hash in
    the unknown-node grey (colour marks an identified node, as in the path graph),
    ``None`` is our own device in white — or, under ``bare_self``, the lone ``★`` — and
    ``dim_from`` fades a resolved tail. The plain rendering
    is character- and style-identical to ``path_text``; what the swap buys is the
    :class:`PathLine` shapes (ellipsized / wrapped) and the powerline mode.

    Args:
        hops: The hops in propagation order — hex hashes, ``None`` marking our own
            device (empty strings are skipped; ``dim_from`` counts rendered hops).
        resolve: Maps a hop hash to a friendly name when known.
        prefix_bytes: Path-hash width an unnamed hop's identity hash presents at under
            ``hash_as_name`` (unnamed hops are otherwise shown grey whole, no prefix lit).
        self_name: Our own node's name — white when a resolved name matches it, and
            naming any ``None`` device hop.
        empty: The muted text shown when there are no hops (e.g. ``"direct"``).
        show_hash: Annotate named hops (and, with ``device_hash``, our device) with
            their hash in parentheses — the trace presentation.
        hash_bytes: Truncate shown/annotated hashes to this byte width.
        device_hash: Our own device's key, annotated onto ``None`` hops when
            ``show_hash`` is on.
        dim_from: Fade hops at/after this rendered index (``None`` dims nothing).
        hash_as_name: Present each unnamed hop as its muted identity hash at the
            ``prefix_bytes`` width, annotated with its addressed byte.
        bare_self: Draw our own device (a ``None`` hop) as the bare
            :data:`SELF_GLYPH`. For a surface whose route *always* begins and ends on
            us, the name and hash on both ends say nothing the reader doesn't know,
            and cost the cells the hops in between need.
        dim_self: Fade the bare stars (the default). On a surface where the route is
            something you *compose*, setting out from us and coming home to us are
            fixtures — no more yours to choose or remove than a mirrored return leg —
            so they wear the same automatic grey, and the colour on the line belongs to
            the nodes actually being picked. Pass ``False`` where nothing is being
            composed (a record of a walk already made): there is no "not yours" to say,
            and our node then reads in the app-wide ``you`` white like everywhere else.
            A ``dim_from`` that reaches a star still fades it either way.
        cursor: Splice an editor's insertion slot (:data:`CURSOR_GLYPH`) in *at* this
            rendered-hop index — the position a chosen hop would take, so index ``0``
            opens the route and ``len`` closes it. Counted over rendered hops exactly
            as ``dim_from`` is, and applied after them, so a slot never shifts what a
            hop shows or fades. ``None`` draws no cursor.
        mode: The :class:`PathLine` mode (``"auto"``/``"powerline"``/``"plain"``).
        from_origin: The first hop is the route's origin (see :class:`PathLine`). Clear
            it where ``hops`` is a chain of *relays* — the raw ``path`` field a packet
            carries names neither the node it came from nor the one it reached.
        to_destination: The last hop is the route's destination, same reading.

    Returns:
        The assembled :class:`PathLine`.
    """
    shown = [h for h in hops if h is None or h]
    built: list[PathHop] = []
    for i, hop in enumerate(shown):
        dim = dim_from is not None and i >= dim_from
        if hop is None:
            if bare_self:
                # Where the route is being composed, both our ends are fixed — a walk
                # leaves us and comes home to us, and neither end is anybody's to choose
                # or remove — so the stars fade like every other automatic hop and the
                # colour belongs to the nodes actually being picked. Where nothing is
                # being composed (``dim_self=False``), that has nothing to say, and our
                # own node keeps the app-wide ``you`` white.
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
        if dim:  # a faded unnamed hop shows its compact hash, exactly as path_text does
            built.append(PathHop(compact, dim=True))
        elif hash_as_name:
            identity = _shorten(hop, prefix_bytes or None)
            note = compact if (show_hash and compact and compact != identity) else None
            built.append(PathHop(identity, annotation=note))
        else:
            # No name resolved: the node is unknown, so its hash carries no key — the hop
            # takes the app-wide unknown-node grey (arrow and chip alike), never a hue its
            # bytes would derive. Colour marks an identified node, as in the path graph.
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
