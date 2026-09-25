# SPDX-License-Identifier: Apache-2.0
"""Shared Rich theme and console factory for a consistent, modern look.

Two palettes, one vocabulary: every style *name* here exists in both
:data:`MESH_THEME` (the regular platform's truecolor look) and :data:`MESH_THEME_16`
(the PicoCalc console's 16-slot look), so screens never know which one is active — they
ask for ``"warn"`` or ``"snr.good"`` and the platform decides what that means. The
active theme, the name-colouring rule, the icon funnel and the render-boundary fold are
all **bound at platform-switch time** through :func:`meshterm.platforms.on_platform`:
the hot render paths read module globals and never re-derive platform state per call.
"""

from __future__ import annotations

import colorsys
import re as _re
from collections.abc import Callable
from functools import lru_cache

from rich.cells import cell_len
from rich.console import Console
from rich.theme import Theme

from ..platforms import Platform, on_platform
from .fontset import FONT_CODEPOINTS

MESH_THEME = Theme(
    {
        "brand": "bold #5eead4",
        "accent": "bold #818cf8",
        # THE reverse-video chip — a dialog's committing button, a running action's Abort,
        # the composer's insertion slot where there is no chip fill to carry it. A *grey*
        # block with the page's own ink showing through, because the cyan it used to be
        # was the wordmark's teal doing a third job (identity, and a node hue, and now a
        # selection), and a chip is chrome: it marks where a press lands, it does not
        # claim a colour. The slate is ``muted``'s, light enough that the reversed ink —
        # whatever the terminal's background happens to be — reads on it. It must be a
        # *single* theme name: Rich silently drops a style string that mixes a theme name
        # with an attribute (e.g. "reverse muted" renders as plain text), so the reverse is
        # baked into the definition here rather than tacked on at the call site.
        "selected": "reverse bold #94a3b8",
        # THE ink of "this is the one you picked" — the row the ❯ points at, in every
        # select list and in every screen that draws its own rows (feed, routes, records,
        # walk, composer), and the lit column heading one axis over where a list is
        # sorted. White, and deliberately *not* ``brand``: the highlight used to borrow
        # the wordmark's teal, which put the app's identity ink and "the row you're on"
        # on the same hue, and worse, teal/cyan is itself a node hue — a cyan-keyed
        # node's name vanished into its own highlight. White is outside the per-node
        # spectrum (see node_style) so it can never collide with an identity. It does
        # share ink with ``you``, which is the accepted cost. Spans that set their own
        # colour keep it over the base — the heat of an age, an SNR reading, a red badge
        # — with the one exception the highlight is *for*: a node's own key-derived hue
        # folds to this white on the cursor row (see theme.is_identity_style and
        # ui.tui.render._whiten_identities), so the row the reader is on reads as one
        # thing rather than as a name competing with its own selection. Bold like
        # ``brand`` was, so the dim-slot ``not bold`` pins that protect a span inside a
        # highlighted row (see MESH_THEME_16) go on meaning what they meant.
        "cursor": "bold #ffffff",
        # THE mark a path line puts around its own hops (PathLine._render), so the cursor
        # row's identity fold can see where a route starts and stops
        # (ui.tui.render._whiten_identities): inside a path line a node's hue is not
        # decoration on a name, it is what tells one hop from the next — and what the
        # graph drawn above the row cross-references, since a graph label is only a
        # marker and one byte of hash. Folding those to white made a picked route read as
        # one long white smear with no way back to the picture. Chips were never affected
        # (their fills sit outside the fold's vocabulary); this is what makes the arrow
        # form — every path line on the PicoCalc, and on any terminal without the
        # powerline glyphs — say the same thing. Renders as nothing: it exists to be read
        # at the render boundary, not seen.
        "pathline": "none",
        # Reversed error (the text-editor cursor sitting on an over-budget character). Baked
        # in for the same reason as ``selected`` — "reverse err" would render as plain text.
        "err.reverse": "reverse bold #f87171",
        # Our own node, anywhere it is named: pure white, deliberately outside the
        # per-node hue spectrum (see node_style) so "you" is always easy to spot.
        "you": "bold #ffffff",
        # A confirmed companion's name on the startup device picker: pure white so the
        # devices we've actually talked to before jump out above the merely-detected ports.
        "device.known": "bold #ffffff",
        # A QR code's modules: pure white ink on a pure black field, both ends named so
        # no palette can dilute the contrast a camera reads (see ui/qr.py). Never black
        # on white — a light-on-dark code is what a scanner expects of a screen.
        "qr": "#ffffff on #000000",
        # The picker's Bluetooth TYPE badge: a white rune on the official Bluetooth blue
        # (Pantone 300, #0057b8), echoing the real logo so BLE reads at a glance.
        "bluetooth": "bold #ffffff on #0057b8",
        # The badge's tapered edges: half-block glyphs drawn in the same blue as the
        # foreground (over the terminal's own background), so only their inner half fills and
        # the badge reads a touch wider than the single rune cell without a hard rectangle.
        "bluetooth.edge": "#0057b8",
        "ok": "bold #4ade80",
        "warn": "bold #fbbf24",
        "err": "bold #f87171",
        "muted": "#94a3b8",
        # A node we can't identify — its ``○`` ring, and its label wherever a bare key or
        # hash stands in for a name (see name_style's keyless return). Its own name rather
        # than plain ``muted`` because the two must not track each other: ``muted`` is
        # chrome and may sit a step down from body text, while an unidentified node is
        # *content* you can still act on. On the console that difference is the whole
        # ladder — chrome takes the dark grey slot, this one the light grey.
        "node.unknown": "#94a3b8",
        # Panel/dialog titles: the same hue as the border they sit in, one shade brighter,
        # so the title reads as part of its frame while still standing out from it. One
        # entry per border style the frame compositor is given (see theme.title_style).
        "title.accent": "bold #a5b4fc",
        "title.muted": "bold #cbd5e1",
        "title.warn": "bold #fcd34d",
        "title.err": "bold #fca5a5",
        "title.ok": "bold #86efac",
        "title.brand": "bold #99f6e4",
        # Panel/dialog footer hints: the border's hue one shade *darker* (and not bold), the
        # mirror image of the ``title.*`` brightening — the hint reads as part of the frame
        # while receding behind it. One entry per border style (see theme.hint_style).
        "hint.accent": "#6366f1",
        "hint.muted": "#64748b",
        "hint.warn": "#f59e0b",
        "hint.err": "#ef4444",
        "hint.ok": "#22c55e",
        "hint.brand": "#2dd4bf",
        # A step darker than ``muted`` for placeholder dashes (a node's missing packet count /
        # age) that should recede below the real, muted values around them.
        "faint": "#64748b",
        # A further step darker than ``faint``, for a meter's unlit track (the SNR quality
        # bars): dark enough to read as background, not as a dimmer version of the reading.
        "track": "#334155",
        "snr.good": "bold #4ade80",
        "snr.ok": "bold #fbbf24",
        "snr.bad": "bold #f87171",
        # The status-bar battery gauge: a filled block, not four thin dots. Every cell is a
        # lit foreground over its own darker ground of the same hue — light green on green
        # over half, yellow on brown over a quarter, light red on red below — so the gauge
        # reads as a *block* at a glance and the braille fill inside it says how much of the
        # pack is left. ``batt.flash`` and ``batt.flash.off`` are the two beats of the
        # last-percent alarm: black dots on red, alternating with the light red on the bare
        # page. Losing the ground for a beat is a bigger change than any hue swap could be,
        # which is what makes it read across a room.
        "batt.full": "bold #4ade80 on #15803d",
        "batt.mid": "bold #fbbf24 on #a16207",
        "batt.low": "bold #f87171 on #991b1b",
        "batt.flash": "not bold #000000 on #dc2626",
        "batt.flash.off": "bold #f87171",
        # The node-type *marks* — THE colours behind ● ▲ ■ ◉ wherever a typed node is
        # drawn (see ui.widgets.NODE_GLYPHS), and the map's own marker language: clients
        # the loud pink, repeaters the calmer violet, rooms a white square, sensors an
        # orange ringed dot. Named styles rather than raw hex so the 16-slot console picks
        # its slot *deliberately* — a naive downsample of the violet lands on grey, which
        # would make a repeater read as an unknown node.
        "type.node": "#f472b6",
        "type.repeater": "#a78bfa",
        "type.room": "#ffffff",
        "type.sensor": "#fb923c",
        # A region name (a flood's scope). Not a node, so no hue from the node wheel: a
        # light slate, set apart from prose by its slant the way a tag is from a sentence.
        "scope": "italic #cbd5e1",
        # The basemap features whose *hue* is the information — water is blue, parks are
        # green, a highway is the warm one — and which a naive downsample therefore ruins
        # (the dark blue lands on grey, the dark green on black, the amber on bright red).
        # Named for the same reason as ``type.*``: the console picks the slot itself. The
        # rest of the basemap is grey by design and quantizes honestly, so minor roads,
        # rail, boundaries and labels stay literal hex in ui.map_render.
        "map.water": "#153b56",
        "map.river": "#49b0ec",
        "map.stream": "#3f8fbf",
        "map.ditch": "#3a7ba6",
        "map.park": "#173a29",
        "map.highway": "#f2a13d",
        # The heard-age heat scale's steps, named for how old the node they colour is:
        # a node heard eight minutes ago is ``heat.minutes``, one heard eight days ago is
        # ``heat.days``. Seven steps from white-hot to cold ash (JP's spec) — a cooling
        # ember, then what's left of it. ``heat.never`` is both "over a year" and "never
        # heard": past a year the distinction stops being worth a colour.
        # The regular platform interpolates a continuous gradient over the same anchors
        # rather than stepping (see ui.widgets._recency_style), so these are its ladder
        # spelled out — every style name resolves on both platforms either way.
        "heat.now": "#ffffff",  # under 5 minutes
        "heat.minutes": "#facc15",  # 5 minutes
        "heat.hours": "#f87171",  # 1 hour
        "heat.days": "#b45309",  # 1 day
        "heat.weeks": "#dc2626",  # 1 week
        "heat.months": "#94a3b8",  # 1 month
        "heat.never": "#64748b",  # 1 year, and never heard
        # The PicoCalc F-key lane's chip fills (see ui.tui.fkeys): gray for the plain
        # F1-F5 bank, green while the Shift watcher reports F6-F10. White text on both —
        # defined here too so the style names resolve on every platform, even though
        # only PicoCalc's footer_fkeys ever asks for them.
        "fkey.chip": "bold #ffffff on #475569",
        "fkey.chip.shift": "bold #ffffff on #16a34a",
        # The prose voices — what a markdown page's inline marks are drawn in (see
        # ui.markdown). Headings borrow the styles the rest of the app already heads
        # sections with (``brand``, ``accent``), so only the *body* marks need names of
        # their own: emphasis brighter than the page, an aside dimmer than it, code and
        # links in the two hues nothing else in a body claims, a struck-out run receding
        # to the placeholder grey, and the bullet/rail chrome a list or a quote hangs on.
        "md.strong": "bold #e2e8f0",
        "md.em": "italic #cbd5e1",
        "md.code": "#7dd3fc",
        "md.link": "underline #a5b4fc",
        "md.quote": "italic #cbd5e1",
        "md.strike": "strike #64748b",
        "md.bullet": "#818cf8",
        "md.rail": "#475569",
    }
)

#: The PicoCalc console's 16 palette slots — the **standard kernel VT palette**, by JP's
#: decision (2026-08-01): the console is not remapped, so other software looks stock and
#: the black background stays black. :data:`MESH_THEME_16` is designed against these
#: RGBs, and the fold's stray-truecolor quantizer matches against them. Rules that still
#: bind:
#:
#: * The kernel VT renders **bold as brightness**: ``bold`` on a 0–7 foreground jumps it
#:   to slot N+8. So a dim-slot style must say what it means about bold — it cannot leave
#:   the question open, because Rich merges a *base* style into every span it wraps and a
#:   selected row's ``bold`` would then recolour the span outright (a light-grey unknown
#:   hash arriving as white "you", a purple repeater as pink). Every dim-slot style is
#:   therefore either explicitly ``not bold`` (the colour is load-bearing; keep it) or
#:   explicitly ``bold`` (the promotion *is* the intent — only ``title.muted``). Never
#:   silent, and never bold on 5/6, whose partners are claimed by *different* semantics
#:   here (5 purple = the repeater mark, 13 pink = the node mark; 6 cyan = hint.brand,
#:   14 bright cyan = brand).
#: * Backgrounds can only address slots 0–7 (SGR 40–47).
#: * The six *chromatic bright* slots (9-14) are the node-name spectrum's landing zone —
#:   the console's rendering of the key-derived hue wheel (see :data:`_NODE_SLOT_HEXES`).
_VT_SLOTS: tuple[tuple[int, str, str], ...] = (
    (0, "background (black)", "#000000"),
    (1, "red (hint.err)", "#aa0000"),
    (2, "green (hint.ok)", "#00aa00"),
    (3, "brown/orange (hint.warn, batt.mid, heat.cool, the sensor mark)", "#aa5500"),
    (4, "blue (hint.accent, bluetooth bg)", "#0000aa"),
    (5, "purple (the repeater mark)", "#aa00aa"),
    (6, "cyan (hint.brand)", "#00aaaa"),
    (7, "light grey (default text, heat.cold)", "#aaaaaa"),
    (8, "dark grey (muted, faint, track)", "#555555"),
    (9, "bright red (err; node hue 0°)", "#ff5555"),
    (10, "bright green (ok; node hue 120°)", "#55ff55"),
    (11, "bright yellow (warn, heat.warm; node hue 60°)", "#ffff55"),
    (12, "bright blue (accent; node hue 240°)", "#5555ff"),
    (13, "bright pink (the node mark — the map's pink, for free; node hue 300°)", "#ff55ff"),
    (14, "bright cyan (brand; node hue 180°)", "#55ffff"),
    (15, "white (you, heat.hot, the room mark)", "#ffffff"),
)

#: The custom 16-slot remap P3 originally shipped (tailwind-family RGBs programmed via
#: ``setvtrgb``) — **archived, not installed**: JP chose the standard palette but asked
#: to keep this in case he changes his mind. Reinstall with
#: ``MESHTERM_CUSTOM_PALETTE=1 sh scripts/picocalc/calculinux-console-font-6x12.sh`` (whose opt-in
#: block a test keeps byte-identical to :func:`vtrgb_lines`); MESH_THEME_16 would then
#: want re-tuning against it (see git history at c6485c6 for the matching theme).
_VT_SLOTS_CUSTOM: tuple[tuple[int, str, str], ...] = (
    (0, "background slate", "#0f172a"),
    (1, "red (hint.err)", "#ef4444"),
    (2, "green (hint.ok)", "#22c55e"),
    (3, "orange", "#f59e0b"),
    (4, "indigo (hint.accent)", "#6366f1"),
    (5, "track slate (was magenta)", "#334155"),
    (6, "faint slate (was dim cyan)", "#64748b"),
    (7, "light grey", "#cbd5e1"),
    (8, "muted grey", "#94a3b8"),
    (9, "err red", "#f87171"),
    (10, "ok green", "#4ade80"),
    (11, "warn amber", "#fbbf24"),
    (12, "accent indigo", "#818cf8"),
    (13, "lavender", "#a5b4fc"),
    (14, "brand teal", "#5eead4"),
    (15, "white", "#ffffff"),
)


def vtrgb_lines() -> str:
    """The ``setvtrgb`` file content for the **archived custom** palette (three CSV lines).

    Not installed by default — see :data:`_VT_SLOTS_CUSTOM`. The deploy script's opt-in
    block carries this same content literally (a test keeps the two in sync), so the
    remap stays one environment variable away without running Python.
    """
    channels = []
    for shift in (16, 8, 0):
        values = [(int(hex_.lstrip("#"), 16) >> shift) & 0xFF for _, _, hex_ in _VT_SLOTS_CUSTOM]
        channels.append(",".join(str(v) for v in values))
    return "\n".join(channels) + "\n"


#: The 16-slot palette theme: the same style names as :data:`MESH_THEME`, expressed as
#: ``color(N)`` references into :data:`_VT_SLOTS` — the **standard** VT palette. Kept
#: literal (rather than derived) so a slot choice is reviewable next to its meaning; the
#: bold-brightness and background rules it must obey are documented on
#: :data:`_VT_SLOTS` and pinned by tests. Notable stock-palette wins: bright pink (13)
#: is the map's client colour for free, and dim purple (5) stands in for the map's
#: repeater violet. The cost: the three grey depths (muted/faint/track) all collapse
#: onto slot 8 — the stock palette has exactly one dark grey.
MESH_THEME_16 = Theme(
    {
        "brand": "bold color(14)",
        "accent": "bold color(12)",
        # Slot 7 is the only grey the VT can put *behind* a reverse: backgrounds stop at
        # the dim bank, and slot 8's dark grey lands on black there. It is the same light
        # grey the F-key lane already fills its chips with, which is the point — one chip
        # look on the device. ``not bold`` because 7 is a dim slot (see the bold rule).
        "selected": "reverse not bold color(7)",
        # Slot 15, the top of the bright bank — so the row's bold cannot promote it
        # further, and every dim-slot span inside it behaves exactly as it did under the
        # old bold slot-14 highlight.
        "cursor": "bold color(15)",
        # The path line's extent mark — see the regular theme. Nothing to draw, but both
        # themes name the same styles, and the fold that reads it runs on both.
        "pathline": "none",
        "err.reverse": "reverse bold color(9)",
        "you": "bold color(15)",
        "device.known": "bold color(15)",
        "qr": "bold color(15) on color(0)",
        "bluetooth": "bold color(15) on color(4)",
        "bluetooth.edge": "not bold color(4)",
        "ok": "bold color(10)",
        "warn": "bold color(11)",
        "err": "bold color(9)",
        "muted": "color(8)",
        # Light grey, a step *above* muted's dark grey: the console has exactly two greys,
        # and an unidentified node's hash is content, not chrome.
        "node.unknown": "not bold color(7)",
        "title.accent": "bold color(12)",
        # The one deliberate promotion: bold takes slot 7 to 15, and near-white is exactly
        # what this title wants (the regular theme spells it #cbd5e1).
        "title.muted": "bold color(7)",
        "title.warn": "bold color(11)",
        "title.err": "bold color(9)",
        "title.ok": "bold color(10)",
        "title.brand": "bold color(14)",
        "hint.accent": "not bold color(4)",
        "hint.muted": "color(8)",
        "hint.warn": "not bold color(3)",
        "hint.err": "not bold color(1)",
        "hint.ok": "not bold color(2)",
        "hint.brand": "not bold color(6)",
        "faint": "color(8)",
        "track": "color(8)",
        "snr.good": "bold color(10)",
        "snr.ok": "bold color(11)",
        "snr.bad": "bold color(9)",
        # Each band is its bright slot over its own dim one — the console has exactly one
        # pair per hue, and backgrounds stop at the dim bank, so the block look is the
        # palette's natural shape rather than a compromise with it.
        "batt.full": "bold color(10) on color(2)",
        "batt.mid": "bold color(11) on color(3)",
        "batt.low": "bold color(9) on color(1)",
        "batt.flash": "not bold color(0) on color(1)",
        "batt.flash.off": "bold color(9)",
        "type.node": "color(13)",
        "type.repeater": "not bold color(5)",
        "type.room": "color(15)",
        "type.sensor": "not bold color(3)",
        # A region name: the VT has no italic and every chromatic slot is a node hue, so
        # the plain light grey — the word "scope" in front of it does the setting apart.
        "scope": "not bold color(7)",
        # The basemap on the console: a water body is the dim blue, every watercourse
        # crossing it the bright blue one rung up (the palette has exactly one of each —
        # a river's depth of shade is a truecolour luxury, and all three waterway shades
        # land on the same rung here), parks the dim green, highways brown. Backgrounds
        # never come into it: the canvas paints these as braille dots in the foreground.
        "map.water": "not bold color(4)",
        "map.river": "color(12)",
        "map.stream": "color(12)",
        "map.ditch": "color(12)",
        "map.park": "not bold color(2)",
        "map.highway": "not bold color(3)",
        # JP's ladder, slot by slot: white-hot, yellow, the ember's light red, brown as it
        # chars, dark red as it dies, then ash — light grey, and cold grey for what may as
        # well never have been heard. Six of the sixteen slots do the whole scale; the
        # three in the dim bank say ``not bold`` or a selected row would jump them a rung.
        "heat.now": "bold color(15)",
        "heat.minutes": "color(11)",
        "heat.hours": "color(9)",
        "heat.days": "not bold color(3)",
        "heat.weeks": "not bold color(1)",
        "heat.months": "not bold color(7)",
        "heat.never": "color(8)",
        # The F-key lane's chip fills: light-grey background (the palette's only "gray"
        # addressable as a background — see _VT_SLOTS) for the plain bank, green for the
        # Shift bank, white text on both.
        "fkey.chip": "bold color(15) on color(7)",
        "fkey.chip.shift": "bold color(15) on color(2)",
        # Prose on the console. The page's body text is the default light grey (slot 7),
        # so emphasis is the one place the VT's bold-is-brightness rule *is* the design:
        # ``md.strong`` says bold on purpose and lands on white, exactly the step up the
        # truecolor theme spells out. Its opposite, ``md.em``, keeps slot 7 by saying so.
        # Code takes the dim cyan (never bold — slot 6's bright partner is the brand),
        # links the bright blue, and the chrome (bullets, rails, a struck run) the two
        # greys, so a page reads as text with landmarks rather than as a colour chart.
        # No underline anywhere: the VT renders it by swapping in the console's own
        # underline colour, which would take the link's hue away from it.
        "md.strong": "bold color(7)",
        "md.em": "not bold color(7)",
        "md.code": "not bold color(6)",
        "md.link": "color(12)",
        "md.quote": "not bold color(7)",
        "md.strike": "color(8)",
        "md.bullet": "color(12)",
        "md.rail": "color(8)",
    }
)


def active_theme() -> Theme:
    """The platform's theme — :data:`MESH_THEME`, or :data:`MESH_THEME_16` on PicoCalc."""
    return _ACTIVE_THEME


def mark_rgb(colour: str) -> tuple[int, int, int]:
    """A marker colour as an RGB triple — for the braille rasters.

    A canvas paints in RGB, not in styles (see :mod:`~meshterm.ui.mapcanvas`), so a marker
    that wants to agree with its Rich-drawn twin resolves the *same colour* through here.
    Takes either encoding the marker language uses: a literal ``#rrggbb`` (the fixed map
    marks in :mod:`~meshterm.ui.marks`) or a theme style name (the node-type marks, which
    are named so each platform picks its own — see the theme's ``type.*``).

    On the 16-slot theme a ``color(N)`` entry resolves to that slot's own :data:`_VT_SLOTS`
    RGB rather than Rich's stock triple: Rich's palette is a shade off ours and the fold's
    quantizer matches against ours, so the round trip would otherwise land the marker on a
    neighbouring slot. Memoized (cleared on a platform switch); a raster asks per node.

    Args:
        colour: ``#rrggbb``, or a style name defined in both themes.

    Returns:
        The ``(r, g, b)`` triple; mid-grey for a style with no colour of its own.
    """
    cached = _STYLE_RGB.get(colour)
    if cached is None:
        if colour.startswith("#"):
            cached = (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))
        else:
            color = _ACTIVE_THEME.styles[colour].color
            if color is None:
                cached = (0xAA, 0xAA, 0xAA)
            elif color.number is not None and color.number < len(_SLOT_RGBS):
                cached = _SLOT_RGBS[color.number]
            else:
                triplet = color.get_truecolor()
                cached = (triplet.red, triplet.green, triplet.blue)
        _STYLE_RGB[colour] = cached
    return cached


def make_console() -> Console:
    """Create the application's themed Rich console.

    On legacy Windows consoles the standard streams default to ``cp1252``, which cannot
    encode the box-drawing and marker glyphs (``◆ ● ★``) the UI uses; this reconfigures
    them to UTF-8 where the runtime supports it so output never raises ``UnicodeEncodeError``.

    Returns:
        A :class:`rich.console.Console` configured with the platform's theme.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - stream not reconfigurable
                pass
    return Console(theme=_ACTIVE_THEME)


def title_style(border_style: str) -> str:
    """Return the title style matching a panel's border: the same hue, brighter.

    Args:
        border_style: The theme name the panel's border is drawn in (``"accent"``,
            ``"warn"``, ...).

    Returns:
        The matching ``title.*`` theme name, or ``border_style`` itself when no brighter
        variant is defined (so an unknown border still gets a consistently-tinted title).
    """
    name = f"title.{border_style}"
    return name if name in MESH_THEME.styles else border_style


def hint_style(border_style: str) -> str:
    """Return the footer-hint style matching a panel's border: the same hue, muted.

    The bottom-border counterpart of :func:`title_style` — where the title brightens the
    border's hue, the hint darkens it, so both read as part of the frame with the right
    emphasis.

    Args:
        border_style: The theme name the panel's border is drawn in (``"accent"``,
            ``"warn"``, ...).

    Returns:
        The matching ``hint.*`` theme name, or ``"muted"`` when no variant is defined (so
        an unknown border keeps the old neutral hint rather than a loud one).
    """
    name = f"hint.{border_style}"
    return name if name in MESH_THEME.styles else "muted"


#: The per-node hue *spectrum*: a node's first key byte maps straight onto the HSV colour
#: wheel — ``0x00`` is red, sweeping the full spectrum round to ``0xff`` — at a fixed
#: saturation and value tuned to stay vivid and readable on the dark theme across every hue.
#: So all 256 first-byte values give 256 distinct hues on a truecolor terminal. The app-wide
#: rule holds on every platform: a node's *name* is always coloured — by this wheel, keyed
#: on the node's key (see :func:`node_style`) so the colour is the node's identity, surviving
#: renames and colouring every surface that knows any prefix of the key identically — and our
#: own node is always the pure-white ``you`` style instead, so "us" never blends into the
#: crowd. Shared by the node lists, the chat transcript, the dashboard feed, and the packet
#: viewer, so one node reads as one colour everywhere.
_NODE_HUE_SAT = 0.65
_NODE_HUE_VAL = 0.95

#: The same wheel at the PicoCalc console's resolution: the six *chromatic bright* palette
#: slots, listed in hue order from red so index ``round(byte / 256 * 6) % 6`` is the sector
#: the byte's hue falls in — 60° apart, an even sixth of the wheel each. Written as the
#: slots' own :data:`_VT_SLOTS` RGBs rather than as ``color(N)`` so the value is still a
#: hex a caller can parse into an RGB (the map canvas and the mesh walk read the hue back
#: out of the style string), and so every downsample on the way out — Rich's, and the
#: fold's :func:`_quantize_sgr` — lands on that exact slot rather than guessing.
# fmt: off
_NODE_SLOT_HEXES: tuple[str, ...] = (
    "#ff5555",   # 9  red      —   0°
    "#ffff55",   # 11 yellow   —  60°
    "#55ff55",   # 10 green    — 120°
    "#55ffff",   # 14 cyan     — 180°
    "#5555ff",   # 12 blue     — 240°
    "#ff55ff",   # 13 magenta  — 300°
)
# fmt: on


def node_style(key: str) -> str:
    """The stable spectrum hue a node's *key* selects — the hash-derived node colour.

    The node's first key byte is mapped straight onto the HSV colour wheel (``0x00`` red,
    sweeping round to ``0xff``), so the colour is the node's identity: it survives a
    rename, and only the first byte picks it, so any prefix a surface happens to hold — a
    2-hex path hop, the stored 12-hex id, the full 64-hex public key — lands on the same
    hue. One node, one colour, however it was learned.

    The *rule* is the platform's only constant here; its resolution is not. The regular
    platform spends the full spectrum (:func:`_node_style_spectrum`, 256 distinct hues at
    a fixed saturation/value); PicoCalc snaps the same hue to the nearest of the console's
    six chromatic slots (:func:`_node_style_quantized`), exactly as the heard-age heat
    scale quantizes its gradient there. Bound at platform-switch time.

    Args:
        key: The node's key/hash as hex (any length ≥ 1 byte, ``0x``/mixed-case tolerated).

    Returns:
        A ``"bold #rrggbb"`` style string.
    """
    return _node_impl(key)


def _key_byte(key: str) -> int:
    """The node's first key byte — the only slice any hue derives from."""
    raw = key.lower().removeprefix("0x")
    try:
        return int(raw[:2], 16)
    except ValueError:  # not hex — fall back to the character sum so *something* stable shows
        return sum(map(ord, raw)) % 256


def _node_style_spectrum(key: str) -> str:
    """Regular platform: the full 256-hue wheel at :data:`_NODE_HUE_SAT`/``_VAL``."""
    r, g, b = colorsys.hsv_to_rgb(_key_byte(key) / 256, _NODE_HUE_SAT, _NODE_HUE_VAL)
    return f"bold #{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _node_style_quantized(key: str) -> str:
    """PicoCalc: the same hue, snapped to its sixth of the wheel (:data:`_NODE_SLOT_HEXES`).

    Deliberate rather than left to a nearest-RGB downsample: the spectrum's pastels sit
    close enough to white that a naive match would drain the loudest hues toward grey,
    and grey is ``muted``'s — an unkeyed sender. Snapping by hue keeps all six families
    saturated and evenly populated (~43 of the 256 first bytes each).
    """
    sector = round(_key_byte(key) / 256 * len(_NODE_SLOT_HEXES)) % len(_NODE_SLOT_HEXES)
    return f"bold {_NODE_SLOT_HEXES[sector]}"


#: Every style string a node's key can mint, on *either* platform: the 256-hue spectrum
#: and the console's six chromatic slots. A surface that has to *recognise* identity ink
#: rather than produce it matches against this — today that is the cursor row, where a
#: name gives way to the highlight's white. Both vocabularies live in the one set so the
#: answer never depends on which platform happens to be bound, and they cannot collide:
#: the spectrum's channels top out at ``0xf2`` (value 0.95) while every console slot
#: spells ``0xff``.
_IDENTITY_STYLES: frozenset[str] = frozenset(
    [_node_style_spectrum(f"{byte:02x}") for byte in range(256)]
    + [f"bold {hex_}" for hex_ in _NODE_SLOT_HEXES]
)


def is_identity_style(style: str) -> bool:
    """Is ``style`` a hue some node's key minted — a *name's* ink rather than the page's?

    :func:`node_style` read backwards, for the one surface that needs the vocabulary as a
    question: the cursor row, whose node names are drawn in its white instead of their own
    hue (see :func:`~meshterm.ui.tui.render.render_to_ansi`). Deliberately narrow — only a
    key-derived hue answers yes, so ``node.unknown``'s grey (a node we *cannot* identify,
    which the highlight has no business claiming to know), the ``you`` white, and every
    context colouring keep exactly what they were given.

    Args:
        style: A style string as it appears on a span.

    Returns:
        ``True`` if it is one of the hues :func:`node_style` mints.
    """
    return style in _IDENTITY_STYLES


def name_style(name: str, key: str | None = None) -> str:
    """The stable colour a node or sender name is drawn in — keyed on the node's key.

    One rule on every platform: the hue is :func:`node_style`'s key-derived spectrum, so a
    rename keeps the colour and every surface that knows any prefix of the key agrees.
    Only the *resolution* changes with the platform (see :func:`node_style`).

    Args:
        name: The display name (unused for the hue; kept so every call site reads
            ``name_style(name, key)`` and the pair stays greppable).
        key: Any known prefix of the node's key/hash. ``None``/empty marks a sender whose
            key we couldn't resolve — drawn ``node.unknown``, because colour is reserved
            for keyed identities (callers with only a name resolve it first via
            :func:`~meshterm.services.trace_runner.make_name_key_resolver`).

    Returns:
        A style for the name; the same node always maps to the same style on a given
        platform, so it keeps its colour across screens and sessions.
    """
    return node_style(key) if key else "node.unknown"


# -- the compact icon language (PicoCalc) ---------------------------------------------

#: Emoji → single console-font character: the PicoCalc icon language. One glyph per
#: concept, chosen from what the 512-glyph font actually holds (see ui.fontset);
#: concepts that only ever share a *family* (outbound ↑, route ∟) share deliberately.
#: The node-type marks (★●▲■◉○) and status marks (✓ ✗ ⚠ ● ○) are already font-native
#: and never appear here. ``glyph()`` consumes this table at explicit icon call sites
#: (a 1-cell lane the screen composes); the render-boundary fold consumes it for
#: everything else, padding to the emoji's measured width so layout survives.
# fmt: off
_GLYPH_MAP: dict[str, str] = {
    # Packet classes (KIND_ICONS)
    "📢": "☼",   # advert — a node radiating its presence
    "📊": "≈",   # telemetry — a waveform of readings
    "📦": "▬",   # packet — a plain slab of payload
    "💬": "¶",   # message — text
    "✅": "✓",   # ack (the ok-family check)
    "❔": "·",   # unknown class — a neutral dot
    # Raw payload classes (PAYLOAD_ICONS); raw ADVERT/ACK reuse ☼/✓ above
    "📥": "↑",   # REQ — a question going out
    "📮": "↓",   # RESPONSE — the answer coming back
    "📩": "→",   # TEXT_MSG (overheard direct message) — text in flight
    "📻": "#",   # GRP_TXT — channel text (the # channel mark)
    "💽": "§",   # GRP_DATA — a data section on a channel
    "🎭": "?",   # ANON_REQ — a request from an unproven identity
    "🧭": "∟",   # PATH — a route with a bend in it
    "🎯": "⌖",   # TRACE — the crosshair (also the map's position mark)
    "🧩": "▒",   # MULTIPART — a frame in fragments
    "🧰": "↨",   # CONTROL — adjustment up-and-down
    # Channel openness (widgets.channel_glyph)
    "＃": "#",   # name-derived channel
    "🌐": "@",   # well-known public channel
    "🔒": "⚿",   # private channel, locked contact (the padlock mark)
    "🔓": "⚿",   # unlock — the same padlock; the row's word says which way it turns
    # Concept icons (menu/list rows)
    "📡": "☼",   # advert tool — same concept as the advert class
    "🕒": "◷",   # clock/sync (the clock-face mark)
    "🔄": "°",   # reboot — the power dot
    "💾": "⌂",   # backup — put it somewhere safe
    "📂": "^",   # restore — bring it back up
    "🔑": "*",   # channel/credential key — masked-secret asterisk
    "🔐": "*",   # identity/auth secret — same secret-material mark
    "🗑": "✗",   # clear/delete — the destructive mark
    "✎": "~",   # compose/edit — a scribble
    "⚡": "!",   # explore/probe
    "⭐": "+",   # watch — added to the watchlist
    "📤": "↑",   # send now (outbound family)
    "📨": "=",   # courier/queue — stacked letters
    "🔔": "•",   # notify — the badge dot
    "🔕": "·",   # mute — the hollowed-out dot
    "📱": "▓",   # QR — a dense block
    "🔗": "&",   # link — the joining glyph
    "🏆": "★",   # trophy case — the best/winner star
    "⌨": "❯",   # command line — the prompt cursor
    "📖": "¶",   # read/about — a page of prose (the text mark, as ¶ is for a message)
    "💰": "$",   # support/donate — the plainest possible money mark
    "🚪": "",    # quit — no icon; the word carries it
    "🌍": "@",   # map/world (globe family)
    "🕸": "∟",   # mesh walk (route family)
    "🚨": "⚠",   # watchtower alert
    "🛣": "∟",   # longest-haul route (route family)
    "🏹": "►",   # longest leg — the arrow in flight (▶ is the run *action*, not this)
    "🧳": "→",   # trip/journey
    "🔆": "°",   # brightest sighting
    "📶": "≥",   # TX power sweep — the power ramp
    "🔧": "⚙",   # config (parameter concept)
    "📋": "i",   # info
    "🩺": "?",   # diagnostics — the question a diagnosis answers (🎭 shares the mark)
    "📰": "…",   # live feed — a stream of items
    "🎧": "≈",   # monitor — listening to the waveform
    "🗼": "▲",   # repeater admin — the repeater mark itself
    "🔌": "~",   # serial port — the cable
    "📍": "╨",   # a radio on the host's SPI bus — the antenna on its board
    "🔖": "╬",   # region/scope — a flood's label; the grid square of an area
    "👤": "%",   # a person (two-circle silhouette)
    "👥": "%",   # contacts — people
    "👋": "",    # a wave in prose — the words carry it
    "⏳": "…",   # pending/waiting
    "＋": "+",   # fullwidth plus (channels' add row)
}
# fmt: on


def glyph(icon: str) -> str:
    """The platform's rendering of an icon: the emoji itself, or its compact glyph.

    On the regular platform this is the identity — emoji icons render as themselves.
    On PicoCalc every icon funnels to a single console-font character via
    :data:`_GLYPH_MAP` (an unmapped icon passes through and is caught by the glyph
    whitelist test / render-boundary fold, not silently invented here). Note the
    compact form is *one cell* where the emoji was two: call sites compose their lanes
    from the returned glyph, so the lane simply tightens on PicoCalc.

    Args:
        icon: An emoji, a node/status mark, or any literal character.

    Returns:
        The character(s) to render for it on the active platform.
    """
    return _glyph_impl(icon)


def _glyph_identity(icon: str) -> str:
    """Regular platform: icons render as themselves."""
    return icon


def _glyph_compact(icon: str) -> str:
    """PicoCalc: icons collapse to their single-cell console-font glyph."""
    return _GLYPH_MAP.get(icon, icon)


# -- the render-boundary fold (PicoCalc) ----------------------------------------------

#: What may survive the fold: every font codepoint, plus the C0 controls the rendered
#: ANSI itself is built from (ESC in its sequences, the newlines between lines).
_FOLD_ALLOWED: frozenset[int] = FONT_CODEPOINTS.union(range(0x00, 0x20))

#: Width-1 characters outside the font with a natural width-1 stand-in. Applied by the
#: fold's translation table (storage is never touched). Characters *in* the font —
#: ``— … ⋯ ⚠ ⌫ ⇧ ⚙ ↻ ◷ ⌖ ⚿ ← ↑ → ↓ ↔ ↕ • ·`` and the Cyrillic block — never appear
#: here: they pass through untranslated.
# fmt: off
_FOLD_SINGLES: dict[str, str] = {
    "–": "-", "−": "-", "‒": "-", "―": "—",
    "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
    "‹": "<", "›": ">", "«": "<", "»": ">",
    "×": "x", "÷": "/", "⁄": "/", "∙": "·", "∘": "·",
    "⇒": "→", "⇐": "←", "⇣": "↓", "⇡": "↑", "↩": "←", "↪": "→",
    "⇄": "↔", "⟷": "↔", "⟺": "↔", "⟲": "↻", "⟳": "↻",
    "✕": "✗", "✖": "✗", "✔": "✓",
    "ᛒ": "B",   # the Bluetooth badge rune
    "œ": "o", "Œ": "O", "æ": "a", "Æ": "A", "ø": "o", "Ø": "O",
    "ß": "s", "þ": "p", "Þ": "P", "ð": "d", "Ð": "D", "đ": "d", "Đ": "D",
    "ł": "l", "Ł": "L", "ı": "i",
    # Powerline path-pill chrome (private-use): caps become half-blocks, the separator
    # a plain wedge.
    "": ">", "": "▌", "": "▐",
    # Zero-width machinery folds away entirely (width 0 → empty keeps cell math exact):
    # VS16, ZWJ, ZWSP.
    "️": "", "‍": "", "​": "",
}
# fmt: on

#: Built lazily on first fold: ``str.translate`` table = accent folds (NFKD, computed
#: over the Latin ranges once) + :data:`_FOLD_SINGLES` + the emoji map padded to each
#: emoji's measured cell width.
_FOLD_TABLE: dict[int, str] | None = None

#: Truecolor / 256-colour SGR sequences embedded in *pre-rendered* ANSI. The rasterizer
#: console downsamples everything it renders itself, but the braille canvases
#: (``ui.mapcanvas``) emit their own truecolor escapes which pass through Rich verbatim
#: — the fold quantizes those to the 16 slots so the contract holds for every byte out.
#: Any SGR sequence, its parameter list captured whole. The quantizer walks the list
#: rather than matching a colour standing alone: Rich writes a style's foreground and
#: background as *one* sequence (``ESC[38;2;255;255;255;48;2;0;0;0m`` — a QR module),
#: and a colour in the middle of such a list is no less a colour for having company.
_SGR = _re.compile(r"\x1b\[([\d;]*)m")

_SLOT_RGBS: tuple[tuple[int, int, int], ...] = tuple(
    (int(h.lstrip("#")[0:2], 16), int(h.lstrip("#")[2:4], 16), int(h.lstrip("#")[4:6], 16))
    for _, _, h in _VT_SLOTS
)

#: (is_background, r, g, b) → the replacement SGR string. The app uses a few dozen
#: distinct colours; this stays tiny.
_SLOT_CACHE: dict[tuple[bool, int, int, int], str] = {}


def _nearest_slot_params(background: bool, r: int, g: int, b: int) -> str:
    """The 16-colour SGR *parameters* closest to ``(r, g, b)`` in the :data:`_VT_SLOTS` palette.

    ``22;31``, ``91``, ``40`` — without the ``ESC[…m`` around them, so the answer can be
    spliced into a sequence that carries other parameters too (:func:`_quantize_sgr`).

    Foregrounds may land on any slot (30–37 / 90–97); backgrounds only on 0–7 (the VT has
    no bright backgrounds), so a bright colour used as a fill picks its dim-bank cousin.

    A **dim-bank foreground states its intent** (``22;3N``, normal intensity) rather than
    emitting a bare ``3N``. Bold is brightness on the VT, and ``9N`` *is* how the console
    spells bright — so a bare ``31`` immediately after a ``90`` inherits the intensity bit
    and silently renders as ``91``. Quantized art is a run of adjacent colour spans with
    no style boundaries to reset between them (the wordmark's every-other-span slate
    bevel; a map raster's neighbouring cells), so the promotion lands mid-row and colours
    half a row wrong: the narrow wordmark's fourth row — its only dim-slot row — came out
    part red, part light red, split at each span that happened to follow the bevel.
    """
    key = (background, r, g, b)
    cached = _SLOT_CACHE.get(key)
    if cached is None:
        candidates = _SLOT_RGBS[:8] if background else _SLOT_RGBS
        slot = min(
            range(len(candidates)),
            key=lambda i: (
                (candidates[i][0] - r) ** 2
                + (candidates[i][1] - g) ** 2
                + (candidates[i][2] - b) ** 2
            ),
        )
        if background:
            sgr = f"{40 + slot}"
        elif slot < 8:
            sgr = f"22;{30 + slot}"
        else:
            sgr = f"{90 + slot - 8}"
        cached = _SLOT_CACHE[key] = sgr
    return cached


def _rgb_of_256(index: int) -> tuple[int, int, int]:
    """The canonical RGB of xterm-256 ``index`` (cube and grayscale ramps)."""
    if index < 16:
        return _SLOT_RGBS[index]
    if index < 232:
        index -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return (steps[index // 36], steps[index // 6 % 6], steps[index % 6])
    grey = 8 + (index - 232) * 10
    return (grey, grey, grey)


def _quantize_sgr(text: str) -> str:
    """Fold any embedded truecolor / 256-colour SGR down to the 16 palette slots.

    Every sequence's parameter list is walked (see :data:`_SGR`), so a colour folds
    whether it stands alone or shares its sequence with a second colour or an attribute.
    """
    if "8;2;" not in text and "8;5;" not in text:
        return text
    return _SGR.sub(_quantize_params, text)


def _quantize_params(match: _re.Match[str]) -> str:
    """One SGR sequence with each ``38/48;2;r;g;b`` and ``38/48;5;n`` in it folded to a slot."""
    params = match.group(1).split(";")
    out: list[str] = []
    i = 0
    while i < len(params):
        head = params[i]
        if head in ("38", "48") and i + 1 < len(params):
            background = head == "48"
            mode = params[i + 1]
            rgb = params[i + 2 : i + 5]
            if mode == "2" and len(rgb) == 3 and all(x.isdigit() for x in rgb):
                out.append(_nearest_slot_params(background, *(int(x) for x in rgb)))
                i += 5
                continue
            if mode == "5" and i + 2 < len(params) and params[i + 2].isdigit():
                out.append(_nearest_slot_params(background, *_rgb_of_256(int(params[i + 2]))))
                i += 3
                continue
        out.append(head)
        i += 1
    return f"\x1b[{';'.join(out)}m"


def _build_fold_table() -> dict[int, str]:
    """Compose the full translation table (see :data:`_FOLD_TABLE`)."""
    import unicodedata

    table: dict[int, str] = {}
    # Accented Latin (the font's base table is Cyrillic-coverage Terminus: *no* accented
    # Latin at all) plus fullwidth forms: NFKD-decompose, drop combining marks, keep a
    # clean single ASCII survivor. é→e, Å→A, ＃→#, ﬁ→(skipped: two chars).
    for first, last in ((0x00A1, 0x024F), (0x1E00, 0x1EFF), (0xFF01, 0xFF5E)):
        for cp in range(first, last + 1):
            if cp in FONT_CODEPOINTS:
                continue
            decomposed = unicodedata.normalize("NFKD", chr(cp))
            base = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
            if len(base) == 1 and base.isascii() and base.isprintable():
                table[cp] = base
    for char, replacement in _FOLD_SINGLES.items():
        table[ord(char)] = replacement
    for emoji, compact in _GLYPH_MAP.items():
        pad = max(0, cell_len(emoji) - cell_len(compact))
        table[ord(emoji)] = compact + " " * pad
    return table


@lru_cache(maxsize=4096)
def _fold_to_font(text: str) -> str:
    """Fold ``text`` down to the console font's inventory, cell widths preserved.

    Three stages, cheapest first: the translation table (accents, symbol stand-ins,
    the emoji map — one C-level pass), then only if something non-ASCII survives, a
    per-character sweep replacing anything still outside the font with ``?`` at the
    character's own cell width. The sweep is the safety net that makes the platform's
    no-wide-glyphs guarantee (see ``session._has_wide_glyph``) true *by construction*:
    an emoji this module has never heard of still leaves as narrow ``?``s, never as a
    tofu box that breaks the frame's cell math. Cached — render output repeats heavily
    frame to frame, and the fold is platform-independent once this impl is bound.
    """
    global _FOLD_TABLE
    if _FOLD_TABLE is None:
        _FOLD_TABLE = _build_fold_table()
    folded = _quantize_sgr(text).translate(_FOLD_TABLE)
    if folded.isascii():
        return folded
    if all(ord(ch) in _FOLD_ALLOWED for ch in folded):
        return folded
    return "".join(ch if ord(ch) in _FOLD_ALLOWED else "?" * max(0, cell_len(ch)) for ch in folded)


def fold_text(text: str) -> str:
    """The render-boundary text filter for the active platform.

    Identity on the regular platform. On PicoCalc, folds any string that is about to be
    drawn — names, message bodies, whole rendered ANSI lines — down to characters the
    console font can shape (see :func:`_fold_to_font`); storage is never touched. ANSI
    escape sequences pass through untouched (they are pure ASCII, and the fold never
    rewrites ASCII). Applied once, centrally, in :func:`meshterm.ui.tui.render.render_to_ansi`
    — individual screens should not need to call it.

    Args:
        text: The text to fold (or pass through).

    Returns:
        The folded text; cell widths are preserved (wide emoji become glyph + pad).
    """
    return _fold_impl(text)


def _no_fold(text: str) -> str:
    """Regular platform: text renders as stored."""
    return text


def snr_style(snr: float | None) -> str:
    """Return a theme style name describing an SNR value's quality.

    Args:
        snr: An SNR reading in dB, or ``None``.

    Returns:
        ``"snr.good"``, ``"snr.ok"``, ``"snr.bad"``, or ``"muted"`` for ``None``.
    """
    if snr is None:
        return "muted"
    if snr >= 5:
        return "snr.good"
    if snr >= -5:
        return "snr.ok"
    return "snr.bad"


# -- platform binding ------------------------------------------------------------------

_ACTIVE_THEME: Theme = MESH_THEME
_node_impl: Callable[[str], str] = _node_style_spectrum
_glyph_impl: Callable[[str], str] = _glyph_identity
_fold_impl: Callable[[str], str] = _no_fold

#: Marker colour → RGB, filled by :func:`mark_rgb` and dropped on a platform switch (the
#: answer is the *active* theme's).
_STYLE_RGB: dict[str, tuple[int, int, int]] = {}


@on_platform
def _bind(platform: Platform) -> None:
    """Bind the theme's platform-dependent choices (runs now and on every switch)."""
    global _ACTIVE_THEME, _node_impl, _glyph_impl, _fold_impl, _FOLD_TABLE
    _ACTIVE_THEME = MESH_THEME if platform.truecolor else MESH_THEME_16
    _node_impl = _node_style_spectrum if platform.truecolor else _node_style_quantized
    _glyph_impl = _glyph_identity if platform.emoji else _glyph_compact
    _fold_impl = _fold_to_font if platform.ascii_fold else _no_fold
    _STYLE_RGB.clear()
    # The fold table's emoji pads are computed with cell_len at build time. Cell widths
    # can be re-measured/patched (the emoji-width calibration on the regular platform),
    # so a platform switch drops the table and cache rather than trusting stale pads.
    _FOLD_TABLE = None
    _fold_to_font.cache_clear()
