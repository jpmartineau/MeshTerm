# SPDX-License-Identifier: Apache-2.0
"""The shared Rich theme and the console factory, for a consistent, modern look.

There are two palettes and one vocabulary. Each style name here is in both
:data:`MESH_THEME` (the truecolor look of the regular platform) and :data:`MESH_THEME_16`
(the 16-slot look of the PicoCalc console). Thus screens do not know which theme is
active. They ask for ``"warn"`` or ``"snr.good"``, and the platform decides what that
means. The active theme, the rule for the colour of names, the icon funnel, and the fold
at the render boundary are all **bound when the platform switches**, through
:func:`meshterm.platforms.on_platform`. The hot render paths read module globals, and
they never find the platform state again for each call.
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
from .fontset import FONT_CODEPOINTS, FONTS

MESH_THEME = Theme(
    {
        "brand": "bold #5eead4",
        "accent": "bold #818cf8",
        # The only reverse-video chip: the committing button of a dialog, the Abort of a
        # running action, and the insertion slot of the composer where no chip fill carries
        # it. It is a *grey* block, and the ink of the page shows through it. It was cyan
        # before. That cyan was the teal of the wordmark with a third job (identity, a
        # node hue, and now a selection). A chip is chrome. It shows where a press lands,
        # and it does not claim a colour. The slate is the slate of ``muted``. It is light
        # enough that the reversed ink reads on it, in any background of the terminal. It
        # must be a *single* theme name. Rich removes without a message a style string that
        # mixes a theme name with an attribute (for example "reverse muted" renders as plain
        # text). Thus the reverse is part of the definition here, and the call site does
        # not add it.
        "selected": "reverse bold #94a3b8",
        # The only ink for "this is the one that the user selected". It is the row that the
        # ❯ points at, in each select list and in each screen that draws its own rows
        # (feed, routes, records, walk, composer). It is also the lit column heading one
        # axis over, where a list is sorted. It is white, and not ``brand`` on purpose. The
        # highlight once used the teal of the wordmark. Then the identity ink of the app and
        # "the row you are on" had the same hue. Also, teal/cyan is a node hue, so the name
        # of a node with a cyan key disappeared into its own highlight. White is outside the
        # spectrum of the nodes (refer to node_style), so it can never be the same as an
        # identity. It has the same ink as ``you``, and we accept that cost. Spans that set
        # their own colour keep it over the base: the heat of an age, an SNR reading, a red
        # badge. The highlight exists for one exception. The hue of a node, which comes from
        # its key, folds to this white on the cursor row (refer to theme.is_identity_style
        # and ui.tui.render._whiten_identities). Thus the row that the user is on reads as
        # one thing, and not as a name that competes with its own selection. It is bold, as
        # ``brand`` was. Thus the ``not bold`` pins of the dim slots (refer to
        # MESH_THEME_16), which protect a span in a highlighted row, keep their meaning.
        "cursor": "bold #ffffff",
        # The only mark that a path line puts around its own hops (PathLine._render). The
        # identity fold of the cursor row (ui.tui.render._whiten_identities) uses it to see
        # where a route starts and stops. In a path line, the hue of a node is not a
        # decoration on a name. It shows the difference between one hop and the next. The
        # graph above the row also cross-references it, because a label of the graph is
        # only a marker and one byte of a hash. When the fold made these hues white, a
        # selected route looked like one long white smear, and the user could not find the
        # way back to the picture. Chips were never affected, because their fills are
        # outside the vocabulary of the fold. This mark makes the arrow form say the same
        # thing. The arrow form is every path line on the PicoCalc, and every path line on
        # a terminal without the powerline glyphs. The mark renders as nothing. The render
        # boundary reads it, and the user does not see it.
        "pathline": "none",
        # Reversed error (the cursor of the text editor on a character over the budget). It
        # is part of the definition for the same reason as ``selected``: "reverse err"
        # renders as plain text.
        "err.reverse": "reverse bold #f87171",
        # Our node, in each place where it has a name: pure white, and outside the spectrum
        # of the node hues on purpose (refer to node_style). Thus the user can always find
        # "you" easily.
        "you": "bold #ffffff",
        # The name of a confirmed companion on the picker for the startup device: pure
        # white. Thus the devices that we talked to before are more visible than the ports
        # that MeshTerm only detected.
        "device.known": "bold #ffffff",
        # The modules of a QR code: pure white ink on a pure black field. Both ends have
        # names, so no palette can reduce the contrast that a camera reads (refer to
        # ui/qr.py). The code is never black on white, because a scanner expects a code
        # that is light on dark on a screen.
        "qr": "#ffffff on #000000",
        # The Bluetooth TYPE badge of the picker: a white rune on the official Bluetooth
        # blue (Pantone 300, #0057b8). It is like the real logo, so the user can see BLE
        # at a glance.
        "bluetooth": "bold #ffffff on #0057b8",
        # The tapered edges of the badge: half-block glyphs in the same blue as the
        # foreground (over the background of the terminal). Only their inner half fills.
        # Thus the badge looks a little wider than the single rune cell, and it has no hard
        # rectangle.
        "bluetooth.edge": "#0057b8",
        "ok": "bold #4ade80",
        "warn": "bold #fbbf24",
        "err": "bold #f87171",
        "muted": "#94a3b8",
        # A node that MeshTerm cannot identify: its ``○`` ring, and its label where a bare
        # key or hash is the name (refer to the return of name_style when there is no key).
        # It has its own name and does not use ``muted``, because the two styles must not
        # change together. ``muted`` is chrome, and it can be a step darker than the body
        # text. An unidentified node is *content* on which the user can still act. On the
        # console this difference is the whole ladder: chrome takes the dark grey slot,
        # and this style takes the light grey slot.
        "node.unknown": "#94a3b8",
        # The titles of panels and dialogs: the same hue as the border that they are in,
        # one shade brighter. Thus the title reads as part of its frame, and it is still
        # different from the frame. There is one entry for each border style that the frame
        # compositor receives (refer to theme.title_style).
        "title.accent": "bold #a5b4fc",
        "title.muted": "bold #cbd5e1",
        "title.warn": "bold #fcd34d",
        "title.err": "bold #fca5a5",
        "title.ok": "bold #86efac",
        "title.brand": "bold #99f6e4",
        # The footer hints of panels and dialogs: the hue of the border, one shade *darker*
        # (and not bold). This is the opposite of the brightening of the ``title.*`` styles.
        # The hint reads as part of the frame, and it stays behind the frame. There is one
        # entry for each border style (refer to theme.hint_style).
        "hint.accent": "#6366f1",
        "hint.muted": "#64748b",
        "hint.warn": "#f59e0b",
        "hint.err": "#ef4444",
        "hint.ok": "#22c55e",
        "hint.brand": "#2dd4bf",
        # A step darker than ``muted``, for placeholder dashes (the missing packet count or
        # age of a node). They must stay behind the real, muted values around them.
        "faint": "#64748b",
        # The ``── Label ──`` of a section heading: in a grouped list, and the day divider
        # of a chat. It is a dark grey, and never the accent of the frame. When the rule of
        # a heading had the colour of the border, it looked like part of the frame that it
        # is in (JP, 2026-10-02). It is bold, so the label is still a landmark at that
        # shade.
        "heading": "bold #64748b",
        # A further step darker than ``faint``, for the unlit track of a meter (the SNR
        # quality bars). It is dark enough to look like a background, and not like a
        # dimmer version of the reading.
        "track": "#334155",
        "snr.good": "bold #4ade80",
        "snr.ok": "bold #fbbf24",
        "snr.bad": "bold #f87171",
        # The battery gauge of the status bar: a filled block, and not four thin dots. Each
        # cell is a lit foreground over its own darker ground of the same hue: light green
        # on green above half, yellow on brown above a quarter, light red on red below.
        # Thus the gauge looks like a *block* at a glance, and the braille fill in it shows
        # how much of the battery is left. ``batt.flash`` and ``batt.flash.off`` are the two
        # beats of the alarm for the last percent: black dots on red, then in turn the light
        # red on the bare page. When the ground is lost for one beat, the change is bigger
        # than a change of hue. Thus the user can see it from across a room.
        "batt.full": "bold #4ade80 on #15803d",
        "batt.mid": "bold #fbbf24 on #a16207",
        "batt.low": "bold #f87171 on #991b1b",
        "batt.flash": "not bold #000000 on #dc2626",
        "batt.flash.off": "bold #f87171",
        # The node-type *marks*: the only colours for ● ▲ ■ ◉ where a node with a type is
        # drawn (refer to ui.widgets.NODE_GLYPHS). They are also the language of the markers
        # of the map: clients are the loud pink, repeaters are the calmer violet, rooms are
        # a white square, and sensors are an orange dot with a ring. They are named styles
        # and not raw hex, so the 16-slot console chooses its slot *on purpose*. A naive
        # downsample of the violet gives grey, and a repeater looks like an unknown node.
        "type.node": "#f472b6",
        "type.repeater": "#a78bfa",
        "type.room": "#ffffff",
        "type.sensor": "#fb923c",
        # A region name (the scope of a flood). It is not a node, so it has no hue from the
        # node wheel. It is a light slate. Its slant makes it different from prose, as a
        # tag is different from a sentence.
        "scope": "italic #cbd5e1",
        # The features of the basemap for which the *hue* is the information: water is
        # blue, parks are green, and a highway is the warm colour. A naive downsample
        # damages them (the dark blue becomes grey, the dark green becomes black, and the
        # amber becomes bright red). They have names for the same reason as ``type.*``: the
        # console chooses the slot itself. The rest of the basemap is grey by design, and
        # it quantizes correctly. Thus minor roads, rail, boundaries, and labels stay
        # literal hex in ui.map_render.
        "map.water": "#153b56",
        "map.river": "#49b0ec",
        "map.stream": "#3f8fbf",
        "map.ditch": "#3a7ba6",
        "map.park": "#173a29",
        "map.highway": "#f2a13d",
        # The steps of the heat scale for the heard age. Each step has the name of the age
        # of the node that it colours: a node that MeshTerm heard eight minutes ago is
        # ``heat.minutes``, and a node that it heard eight days ago is ``heat.days``. There
        # are seven steps, from white-hot to cold ash (JP's spec). It is a cooling ember,
        # and then what is left of it. ``heat.never`` is both "over a year" and "never
        # heard", because after one year a different colour is not worth the difference.
        # The regular platform interpolates a continuous gradient over the same anchors
        # and does not use steps (refer to ui.widgets._recency_style). Thus these styles
        # are its ladder, written out. Each style name resolves on both platforms in
        # both cases.
        "heat.now": "#ffffff",  # under 5 minutes
        "heat.minutes": "#facc15",  # 5 minutes
        "heat.hours": "#f87171",  # 1 hour
        "heat.days": "#b45309",  # 1 day
        "heat.weeks": "#dc2626",  # 1 week
        "heat.months": "#94a3b8",  # 1 month
        "heat.never": "#64748b",  # 1 year, and never heard
        # The chip fills of the F-key lane of the PicoCalc (refer to ui.tui.fkeys): grey for
        # the plain F1-F5 bank, and green while the Shift watcher reports F6-F10. The text
        # is white on both. They are also defined here so that the style names resolve on
        # each platform, although only footer_fkeys of the PicoCalc asks for them.
        "fkey.chip": "bold #ffffff on #475569",
        "fkey.chip.shift": "bold #ffffff on #16a34a",
        # The fills of the Cardputer Zero deck, taken from its own keyboard (JP,
        # 2026-09-30). The normal fill is the orange-red in which the fn key and its F4–F8
        # legends are printed. While Shift is held, the fill is the dark blue of the Shift
        # key in the product photo of M5. White text on the #ff6633 of the print has a
        # contrast of only 2.9:1. Thus the chip is that colour, a step darker in OKLab at
        # the same hue and chroma (L 0.70 → 0.62, h 38°). The contrast is 4.0:1, and the
        # colour is still a vermilion. The chroma is in the gamut all the way down, so
        # nothing makes it a grey brown.
        "fkey.chip.cardputer_zero": "bold #ffffff on #e34b0f",
        "fkey.chip.cardputer_zero.shift": "bold #ffffff on #0f72bd",
        # The prose voices: the styles of the inline marks of a markdown page (refer to
        # ui.markdown). Headings use the styles that the rest of the app already uses for
        # section headings (``brand``, ``accent``). Thus only the *body* marks need their own
        # names. Emphasis is brighter than the page. An aside is dimmer than the page. Code
        # and links have the two hues that nothing else in a body uses. A struck-out run
        # goes back to the placeholder grey. The bullet and the rail are the chrome on which
        # a list or a quote hangs.
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

#: The 16 palette slots of the PicoCalc console. They are the **standard kernel VT
#: palette**, by the decision of JP (2026-08-01): the console is not remapped, so other
#: software looks stock and the black background stays black. :data:`MESH_THEME_16` is
#: designed for these RGB values, and the quantizer of the fold for stray truecolor
#: matches against them. These rules still apply:
#:
#: * The kernel VT renders **bold as brightness**. ``bold`` on a foreground in slots 0–7
#:   moves it to slot N+8. Thus a style for a dim slot must say what it means about
#:   bold. It cannot leave the question open, because Rich merges a *base* style into
#:   each span that it wraps. Then the ``bold`` of a selected row changes the colour of
#:   the span completely (a light-grey unknown hash becomes the white "you", and a purple
#:   repeater becomes pink). Thus each style for a dim slot is explicitly ``not bold``
#:   (the colour is necessary, so keep it) or explicitly ``bold`` (the promotion is the
#:   intent, and only ``title.muted`` has it). A style is never silent. Never use bold on
#:   5 and 6, because *different* meanings use their partners here (5 purple = the
#:   repeater mark, 13 pink = the node mark. 6 cyan = hint.brand, 14 bright cyan = brand).
#: * Backgrounds can use only slots 0–7 (SGR 40–47).
#: * The six *chromatic bright* slots (9-14) are the place where the spectrum of node
#:   names lands. They are the console rendering of the hue wheel that comes from the key
#:   (refer to :data:`_NODE_SLOT_HEXES`).
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


def slot_hex(slot: int) -> str:
    """The ``#rrggbb`` of one console palette slot (:data:`_VT_SLOTS`)."""
    return _VT_SLOTS[slot][2]


#: Each bright console slot and its twin in the dim bank: slot ``n + 8`` and slot ``n``.
#: The VT draws a background only from slots 0 to 7. Thus, where a bright colour must
#: become a fill, its dim twin is the only background of the same hue. The path chips
#: use this pair on the console (:mod:`~meshterm.ui.pathline`): the dim twin is the fill,
#: and the bright slot is the text on it.
DIM_TWIN: dict[str, str] = {slot_hex(n + 8): slot_hex(n) for n in range(8)}

#: The custom 16-slot remap that P3 shipped first (tailwind-family RGB values that
#: ``setvtrgb`` programs). It is **archived and not installed**. JP chose the standard
#: palette, but he asked us to keep this remap in case he changes his mind. To install it
#: again, run
#: ``MESHTERM_CUSTOM_PALETTE=1 sh scripts/picocalc-lyra/calculinux-console-font-6x12.sh``
#: (a test makes sure that its opt-in block is byte-identical to :func:`vtrgb_lines`).
#: Then MESH_THEME_16 needs a new adjustment for this remap (refer to the git history at
#: c6485c6 for the matching theme).
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

    MeshTerm does not install it by default (refer to :data:`_VT_SLOTS_CUSTOM`). The
    opt-in block of the deploy script has this same content as literal text (a test makes
    sure that the two are the same). Thus the remap needs only one environment variable,
    and it does not need Python.
    """
    channels = []
    for shift in (16, 8, 0):
        values = [(int(hex_.lstrip("#"), 16) >> shift) & 0xFF for _, _, hex_ in _VT_SLOTS_CUSTOM]
        channels.append(",".join(str(v) for v in values))
    return "\n".join(channels) + "\n"


#: The 16-slot palette theme. It has the same style names as :data:`MESH_THEME`, written
#: as ``color(N)`` references into :data:`_VT_SLOTS`, which is the **standard** VT
#: palette. The theme is literal and not derived, so a reviewer can see each slot choice
#: next to its meaning. The rules for bold-brightness and for backgrounds, which the
#: theme must obey, are in the documentation of :data:`_VT_SLOTS`, and tests pin them.
#: The stock palette gives two important results. Bright pink (13) is the colour of the
#: clients on the map, at no cost. Dim purple (5) replaces the violet of the repeaters
#: on the map. The cost is that the three greys (muted, faint, track) all become slot 8,
#: because the stock palette has only one dark grey.
MESH_THEME_16 = Theme(
    {
        "brand": "bold color(14)",
        "accent": "bold color(12)",
        # Slot 7 is the only grey that the VT can put *behind* a reverse. Backgrounds stop at
        # the dim bank, and the dark grey of slot 8 becomes black there. It is the same
        # light grey that the F-key lane already uses to fill its chips. This is the
        # purpose: the handheld has one chip look. It is ``not bold`` because 7 is a dim
        # slot (refer to the bold rule).
        "selected": "reverse not bold color(7)",
        # Slot 15 is the top of the bright bank. Thus the bold of the row cannot promote it
        # more, and each span for a dim slot in the row behaves as it did under the old
        # highlight, which was bold slot 14.
        "cursor": "bold color(15)",
        # The mark for the extent of a path line (refer to the regular theme). It draws
        # nothing. Both themes have the same style names, and the fold that reads it runs on
        # both.
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
        # Light grey, a step *above* the dark grey of muted. The console has only two greys,
        # and the hash of an unidentified node is content, not chrome.
        "node.unknown": "not bold color(7)",
        "title.accent": "bold color(12)",
        # The one promotion on purpose: bold moves slot 7 to 15. This title needs a colour
        # that is almost white (the regular theme writes it as #cbd5e1).
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
        # The one dark grey of the console. Slot 8 is already in the bright bank, so bold
        # changes nothing.
        "heading": "bold color(8)",
        "track": "color(8)",
        "snr.good": "bold color(10)",
        "snr.ok": "bold color(11)",
        "snr.bad": "bold color(9)",
        # Each band is its bright slot over its own dim slot. The console has only one pair
        # for each hue, and backgrounds stop at the dim bank. Thus the block look is the
        # natural shape of the palette, and it is not a compromise.
        "batt.full": "bold color(10) on color(2)",
        "batt.mid": "bold color(11) on color(3)",
        "batt.low": "bold color(9) on color(1)",
        "batt.flash": "not bold color(0) on color(1)",
        "batt.flash.off": "bold color(9)",
        "type.node": "color(13)",
        "type.repeater": "not bold color(5)",
        "type.room": "color(15)",
        "type.sensor": "not bold color(3)",
        # A region name: the VT has no italic, and each chromatic slot is a node hue. Thus
        # the style is the plain light grey. The word "scope" in front of it makes it
        # different from other text.
        "scope": "not bold color(7)",
        # The basemap on the console: a body of water is the dim blue, and each watercourse
        # that crosses it is the bright blue, one step up. (The palette has only one of
        # each. A depth of shade for a river is a luxury of truecolour, and all three
        # shades of waterway are the same step here.) Parks are the dim green, and highways
        # are brown. Backgrounds are not used here, because the canvas paints these as
        # braille dots in the foreground.
        "map.water": "not bold color(4)",
        "map.river": "color(12)",
        "map.stream": "color(12)",
        "map.ditch": "color(12)",
        "map.park": "not bold color(2)",
        "map.highway": "not bold color(3)",
        # The ladder of JP, slot by slot: white-hot, yellow, the light red of the ember,
        # brown as it chars, dark red as it dies, then ash (light grey), and cold grey for
        # a node that is the same as never heard. Six of the sixteen slots make the whole
        # scale. The three styles in the dim bank say ``not bold``. If they do not, a
        # selected row moves them up one step.
        "heat.now": "bold color(15)",
        "heat.minutes": "color(11)",
        "heat.hours": "color(9)",
        "heat.days": "not bold color(3)",
        "heat.weeks": "not bold color(1)",
        "heat.months": "not bold color(7)",
        "heat.never": "color(8)",
        # The chip fills of the F-key lane: a light-grey background for the plain bank (the
        # only grey in the palette that a background can use, refer to _VT_SLOTS), green for
        # the Shift bank, and white text on both.
        "fkey.chip": "bold color(15) on color(7)",
        "fkey.chip.shift": "bold color(15) on color(2)",
        # The fills of the Cardputer deck, so that the names are the same on both. That
        # display is truecolor, so these are only the nearest slots to its colours: red for
        # the orange of the fn key, and blue for the blue of Shift.
        "fkey.chip.cardputer_zero": "bold color(15) on color(1)",
        "fkey.chip.cardputer_zero.shift": "bold color(15) on color(4)",
        # Prose on the console. The body text of the page is the default light grey (slot
        # 7). Thus emphasis is the one place where the rule of the VT, bold is brightness,
        # is the design. ``md.strong`` says bold on purpose and becomes white. This is the
        # same step up that the truecolor theme writes out. Its opposite, ``md.em``, keeps
        # slot 7 because it says so. Code uses the dim cyan (never bold, because the bright
        # partner of slot 6 is the brand). Links use the bright blue. The chrome (bullets,
        # rails, a struck-out run) uses the two greys. Thus a page reads as text with
        # landmarks and not as a colour chart. There is no underline anywhere. The VT
        # renders it with the own underline colour of the console, and this removes the hue
        # of the link.
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
    """The theme of the platform: :data:`MESH_THEME`, or :data:`MESH_THEME_16` on the PicoCalc."""
    return _ACTIVE_THEME


def mark_rgb(colour: str) -> tuple[int, int, int]:
    """A marker colour as an RGB triple, for the braille rasters.

    A canvas paints in RGB and not in styles (refer to :mod:`~meshterm.ui.mapcanvas`). A
    marker that must agree with its twin that Rich draws gets the *same colour* through
    this function. The function accepts both encodings that the marker language uses. One
    is a literal ``#rrggbb`` (the fixed map marks in :mod:`~meshterm.ui.marks`). The other
    is a theme style name (the node-type marks, which have names so that each platform
    chooses its own colour, refer to ``type.*`` of the theme).

    On the 16-slot theme, a ``color(N)`` entry gives the own :data:`_VT_SLOTS` RGB of that
    slot, and not the stock triple of Rich. The palette of Rich is a shade different from
    ours, and the quantizer of the fold matches against ours. If the function used the
    stock triple, the round trip would put the marker on a neighbouring slot. The
    function caches its results (a platform switch clears the cache). A raster asks for
    each node.

    Args:
        colour: ``#rrggbb``, or a style name that both themes define.

    Returns:
        The ``(r, g, b)`` triple. For a style that has no colour of its own, it is mid-grey.
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
    """Create the Rich console of the application, with its theme.

    On legacy Windows consoles, the standard streams use ``cp1252`` by default. It cannot
    encode the box-drawing and marker glyphs (``◆ ● ★``) that the UI uses. This function
    sets the streams to UTF-8 where the runtime lets it, so that the output never raises
    ``UnicodeEncodeError``.

    Returns:
        A :class:`rich.console.Console` configured with the platform's theme.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - the stream cannot be set again
                pass
    return Console(theme=_ACTIVE_THEME)


def title_style(border_style: str) -> str:
    """Return the title style that matches the border of a panel: the same hue, brighter.

    Args:
        border_style: The theme name in which the border of the panel is drawn
            (``"accent"``, ``"warn"``, and other names).

    Returns:
        The matching ``title.*`` theme name. If no brighter variant is defined, it is
        ``border_style`` itself. Thus an unknown border still gets a title with a
        consistent tint.
    """
    name = f"title.{border_style}"
    return name if name in MESH_THEME.styles else border_style


def hint_style(border_style: str) -> str:
    """Return the footer-hint style that matches the border of a panel: the same hue, muted.

    This is the counterpart of :func:`title_style` for the bottom border. The title
    brightens the hue of the border, and the hint darkens it. Thus both read as part of
    the frame, with the correct emphasis.

    Args:
        border_style: The theme name in which the border of the panel is drawn
            (``"accent"``, ``"warn"``, and other names).

    Returns:
        The matching ``hint.*`` theme name. If no variant is defined, it is ``"muted"``.
        Thus an unknown border keeps the old neutral hint, and not a loud hint.
    """
    name = f"hint.{border_style}"
    return name if name in MESH_THEME.styles else "muted"


#: The per-node hue *spectrum*. The first key byte of a node maps directly onto the HSV
#: colour wheel. ``0x00`` is red, and the full spectrum goes round to ``0xff``. The
#: saturation and the value are fixed, and they are tuned to stay vivid and readable on
#: the dark theme in all hues. Thus the 256 values of the first byte give 256 different
#: hues on a truecolor terminal. The rule of the whole app is the same on each platform:
#: the *name* of a node always has a colour. The colour comes from this wheel, and the key
#: of the node selects it (refer to :func:`node_style`). Thus the colour is the identity
#: of the node. It stays the same after a rename, and it is the same on each surface that
#: knows any prefix of the key. Our node always has the pure-white ``you`` style instead,
#: so "us" never merges into the crowd. The node lists, the chat transcript, the feed of
#: the dashboard, and the packet viewer share this wheel. Thus one node has one colour
#: everywhere.
_NODE_HUE_SAT = 0.65
_NODE_HUE_VAL = 0.95

#: The same wheel at the resolution of the PicoCalc console: the six *chromatic bright*
#: palette slots. They are in hue order, from red. Thus the index ``round(byte / 256 * 6)
#: % 6`` is the sector in which the hue of the byte is. The sectors are 60° apart, and
#: each is an equal sixth of the wheel. They are written as the own :data:`_VT_SLOTS` RGB
#: values of the slots and not as ``color(N)``. Thus the value is still a hex that a
#: caller can parse into an RGB (the map canvas and the mesh walk read the hue from the
#: style string). Also, each downsample on the way out (the downsample of Rich, and
#: :func:`_quantize_sgr` of the fold) gives exactly that slot, and does not guess.
# fmt: off
_NODE_SLOT_HEXES: tuple[str, ...] = (
    "#ff5555",   # 9  red      :   0°
    "#ffff55",   # 11 yellow   :  60°
    "#55ff55",   # 10 green    : 120°
    "#55ffff",   # 14 cyan     : 180°
    "#5555ff",   # 12 blue     : 240°
    "#ff55ff",   # 13 magenta  : 300°
)
# fmt: on


def node_style(key: str) -> str:
    """The stable spectrum hue that the *key* of a node selects: the node colour from its hash.

    The first key byte of the node maps directly onto the HSV colour wheel (``0x00`` is
    red, and the wheel goes round to ``0xff``). Thus the colour is the identity of the
    node. It stays the same after a rename. Only the first byte selects it, so each prefix
    that a surface has gives the same hue. A prefix can be a 2-hex path hop, the stored
    12-hex id, or the full 64-hex public key. One node has one colour, in whatever way
    MeshTerm learned it.

    The *rule* is the only constant of the platform here, but the resolution is not. The
    regular platform uses the full spectrum (:func:`_node_style_spectrum`, 256 different
    hues at a fixed saturation and value). The PicoCalc snaps the same hue to the nearest
    of the six chromatic slots of the console (:func:`_node_style_quantized`). The heat
    scale of the heard age quantizes its gradient in the same way there. MeshTerm binds
    this function when the platform switches.

    Args:
        key: The key or hash of the node as hex (any length of 1 byte or more. The
            function accepts ``0x`` and mixed case).

    Returns:
        A ``"bold #rrggbb"`` style string.
    """
    return _node_impl(key)


def _key_byte(key: str) -> int:
    """The first key byte of the node: the only slice from which each hue comes."""
    raw = key.lower().removeprefix("0x")
    try:
        return int(raw[:2], 16)
    except ValueError:  # not hex: use the sum of the characters, so that a stable colour shows
        return sum(map(ord, raw)) % 256


def _node_style_spectrum(key: str) -> str:
    """The regular platform: the full 256-hue wheel at :data:`_NODE_HUE_SAT`/``_VAL``."""
    r, g, b = colorsys.hsv_to_rgb(_key_byte(key) / 256, _NODE_HUE_SAT, _NODE_HUE_VAL)
    return f"bold #{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _node_style_quantized(key: str) -> str:
    """The PicoCalc: the same hue, snapped to its sixth of the wheel (:data:`_NODE_SLOT_HEXES`).

    This is on purpose, and a downsample to the nearest RGB does not do it. The pastels of
    the spectrum are near white. A naive match moves the loudest hues toward grey, and
    grey is the colour of ``muted``, which is for a sender without a key. A snap by hue
    keeps all six families saturated and evenly populated (approximately 43 of the 256
    first bytes for each family).
    """
    sector = round(_key_byte(key) / 256 * len(_NODE_SLOT_HEXES)) % len(_NODE_SLOT_HEXES)
    return f"bold {_NODE_SLOT_HEXES[sector]}"


#: All the style strings that the key of a node can make, on *each* platform: the 256-hue
#: spectrum and the six chromatic slots of the console. A surface that must *recognize*
#: identity ink, and does not make it, matches against this set. Today this is the cursor
#: row, where a name gives way to the white of the highlight. Both vocabularies are in the
#: one set. Thus the answer does not depend on the platform that is bound, and the two
#: vocabularies cannot collide: the channels of the spectrum have a maximum of ``0xf2``
#: (value 0.95), and each console slot has ``0xff``.
_IDENTITY_STYLES: frozenset[str] = frozenset(
    [_node_style_spectrum(f"{byte:02x}") for byte in range(256)]
    + [f"bold {hex_}" for hex_ in _NODE_SLOT_HEXES]
)


def is_identity_style(style: str) -> bool:
    """Check if ``style`` is a hue that the key of a node made (the ink of a *name*).

    This function is :func:`node_style` read in reverse. One surface needs the vocabulary
    as a question: the cursor row, which draws node names in its white and not in their
    own hue (refer to :func:`~meshterm.ui.tui.render.render_to_ansi`). The check is narrow
    on purpose. Only a hue that comes from a key gives ``True``. Thus the grey of
    ``node.unknown`` (a node that MeshTerm *cannot* identify, and the highlight must not
    claim to know it), the white of ``you``, and each colouring for a context keep the
    colour that they have.

    Args:
        style: A style string as it appears on a span.

    Returns:
        ``True`` if it is one of the hues that :func:`node_style` makes.
    """
    return style in _IDENTITY_STYLES


def name_style(name: str, key: str | None = None) -> str:
    """The stable colour of the name of a node or sender. The key of the node selects it.

    The rule is the same on each platform. The hue is the spectrum of :func:`node_style`
    that comes from the key. Thus a rename keeps the colour, and each surface that knows
    any prefix of the key gives the same colour. Only the *resolution* changes with the
    platform (refer to :func:`node_style`).

    Args:
        name: The display name. It does not change the hue. It is a parameter so that each
            call site reads ``name_style(name, key)``, and a search finds the pair.
        key: Any known prefix of the key or hash of the node. ``None`` or an empty value
            marks a sender whose key MeshTerm could not resolve. The name is drawn as
            ``node.unknown``, because colour is only for identities that have a key.
            (A caller that has only a name must first resolve it with
            :func:`~meshterm.services.trace_runner.make_name_key_resolver`.)

    Returns:
        A style for the name. On one platform, the same node always has the same style.
        Thus it keeps its colour across screens and sessions.
    """
    return node_style(key) if key else "node.unknown"


# -- the compact icon language (PicoCalc) ---------------------------------------------

#: The map from an emoji to a single console-font character. It is the icon language of
#: the PicoCalc. There is one glyph for each concept, chosen from the glyphs that the
#: 512-glyph font has (refer to ui.fontset). Concepts that only share a *family* (outbound
#: ↑, route ∟) share a glyph on purpose. The node-type marks (★●▲■◉○) and the status marks
#: (✓ ✗ ⚠ ● ○) are already native to the font, and they are never in this table.
#: ``glyph()`` uses this table at the explicit icon call sites (a lane of 1 cell that the
#: screen composes). The fold at the render boundary uses it for all other cases. It adds
#: padding to the measured width of the emoji, so that the layout stays correct.
# fmt: off
_GLYPH_MAP: dict[str, str] = {
    # Packet classes (KIND_ICONS)
    "📢": "☼",   # advert: a node that radiates its presence
    "📊": "≈",   # telemetry: a waveform of readings
    "📦": "▬",   # packet: a plain slab of payload
    "💬": "¶",   # message: text
    "✅": "✓",   # ack (the ok-family check)
    "❔": "·",   # unknown class: a neutral dot
    # Raw payload classes (PAYLOAD_ICONS). Raw ADVERT and ACK use ☼ and ✓ above
    "📥": "↑",   # REQ: a question that goes out
    "📮": "↓",   # RESPONSE: the answer that comes back
    "📩": "→",   # TEXT_MSG (an overheard direct message): text in flight
    "📻": "#",   # GRP_TXT: channel text (the # channel mark)
    "💽": "§",   # GRP_DATA: a data section on a channel
    "🎭": "?",   # ANON_REQ: a request from an identity that is not proven
    "🧭": "∟",   # PATH: a route with a bend in it
    "🎯": "⌖",   # TRACE: the crosshair (also the position mark of the map)
    "🧩": "▒",   # MULTIPART: a packet in fragments
    "🧰": "↨",   # CONTROL: adjustment up and down
    # Channel openness (widgets.channel_glyph)
    "＃": "#",   # name-derived channel
    "🌐": "@",   # well-known public channel
    "🔒": "⚿",   # private channel, locked contact (the padlock mark)
    "🔓": "⚿",   # unlock: the same padlock. The word of the row says which way it turns
    # Concept icons (menu/list rows)
    "📡": "☼",   # advert tool: the same concept as the advert class
    "🕒": "◷",   # clock/sync (the clock-face mark)
    "🔄": "°",   # reboot: the power dot
    "💾": "⌂",   # backup: put it in a safe place
    "📂": "^",   # restore: bring it back up
    "🔑": "*",   # channel/credential key: the asterisk of a masked secret
    "🔐": "*",   # identity/auth secret: the same mark for secret material
    "🗑": "✗",   # clear/delete: the destructive mark
    "✎": "~",   # compose/edit: a scribble
    "⚡": "!",   # explore/probe
    "⭐": "+",   # watch: added to the watchlist
    "📤": "↑",   # send now (outbound family)
    "📨": "=",   # courier/queue: stacked letters
    "🔔": "•",   # notify: the badge dot
    "🔕": "·",   # mute: the hollowed-out dot
    "📱": "▓",   # QR: a dense block
    "🔗": "&",   # link: the joining glyph
    "🏆": "★",   # trophy case: the best/winner star
    "⌨": "❯",   # command line: the prompt pointer
    "📖": "¶",   # read/about: a page of prose (the text mark, as ¶ is for a message)
    "💰": "$",   # support/donate: the plainest money mark
    "🚪": "",    # quit: no icon, because the word carries it
    "🌍": "@",   # map/world (globe family)
    "🕸": "∟",   # mesh walk (route family)
    "🚨": "⚠",   # watchtower alert
    "🛣": "∟",   # longest-haul route (route family)
    "🏹": "►",   # longest leg: the arrow in flight (▶ is the run *action*, not this)
    "🧳": "→",   # trip/journey
    "🔆": "°",   # the brightest reception
    "📶": "≥",   # TX power sweep: the power ramp
    "🔧": "⚙",   # config (parameter concept)
    "📋": "i",   # info
    "🩺": "?",   # diagnostics: the question that a diagnosis answers (🎭 has the same mark)
    "📰": "…",   # live feed: a stream of items
    "🎧": "≈",   # monitor: listening to the waveform
    "🗼": "▲",   # repeater admin: the repeater mark itself
    "📌": "■",   # rooms: the room server mark itself
    "🔌": "~",   # serial port: the cable
    "📍": "╨",   # a radio on the SPI bus of the host: the antenna on its board
    "🔖": "╬",   # region/scope: the label of a flood. It is the grid square of an area
    "👤": "%",   # a person (two-circle silhouette)
    "👥": "%",   # contacts: people
    "👋": "",    # a wave in prose: the words carry it
    "⏳": "…",   # pending/waiting
    "＋": "+",   # fullwidth plus (the add row of the channels)
}
# fmt: on


def glyph(icon: str) -> str:
    """The rendering of an icon on the platform: the emoji itself, or its compact glyph.

    On the regular platform, this function returns the icon unchanged, and emoji icons
    render as themselves. On the PicoCalc, each icon goes through :data:`_GLYPH_MAP` to a
    single console-font character. An icon that is not in the map passes through. The test
    for the glyph whitelist or the fold at the render boundary finds it. This function
    does not invent a glyph without a message. The compact form is *one cell*, where the
    emoji was two cells. Call sites compose their lanes from the returned glyph, so the
    lane becomes narrower on the PicoCalc.

    Args:
        icon: An emoji, a node/status mark, or any literal character.

    Returns:
        The characters to render for it on the active platform.
    """
    return _glyph_impl(icon)


def _glyph_identity(icon: str) -> str:
    """The regular platform: icons render as themselves."""
    return icon


def _glyph_compact(icon: str) -> str:
    """The PicoCalc: icons become their single-cell console-font glyph."""
    return _GLYPH_MAP.get(icon, icon)


# -- the render-boundary fold (the handhelds) ------------------------------------------

#: The glyph inventory of the active platform (``Platform.font``). :func:`_bind` binds it.
#: It is the 512-glyph console font of the PicoCalc, or what the emulator of the Cardputer
#: draws.
_FOLD_FONT: frozenset[int] = FONT_CODEPOINTS

#: The characters that the fold lets stay: each font codepoint, and the C0 controls from
#: which the rendered ANSI is made (ESC in its sequences, and the newlines between lines).
_FOLD_ALLOWED: frozenset[int] = _FOLD_FONT.union(range(0x00, 0x20))

#: Shows if the fold also quantizes embedded truecolor and 256-colour SGR to the 16 slots.
#: It does this for a console that has no more colours (the PicoCalc). It never does this
#: for a display that the host paints in 24 bits.
_FOLD_QUANTIZE = True

#: The characters of width 1 that are not in the font and have a natural stand-in of width
#: 1. The translation table of the fold applies them (MeshTerm never changes the stored
#: data). The characters that are *in* the font are never here, and they pass through
#: without a change. These are ``— … ⋯ ⚠ ⌫ ⇧ ⚙ ↻ ◷ ⌖ ⚿ ← ↑ → ↓ ↔ ↕ • ·`` and the Cyrillic
#: block.
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
    # Chrome of the powerline path pills (private use): the caps become half-blocks, and
    # the separator becomes a plain wedge.
    "": ">", "": "▌", "": "▐",
    # The zero-width characters fold away completely (width 0 becomes empty, and the
    # arithmetic of the cells stays exact): VS16, ZWJ, ZWSP.
    "️": "", "‍": "", "​": "",
}
# fmt: on

#: MeshTerm builds it lazily, at the first fold. It is the ``str.translate`` table. It has
#: the accent folds (NFKD, computed one time over the Latin ranges), :data:`_FOLD_SINGLES`,
#: and the emoji map padded to the measured cell width of each emoji.
_FOLD_TABLE: dict[int, str] | None = None

#: The SGR sequences for truecolor and 256 colours that are embedded in *pre-rendered*
#: ANSI. The rasterizer console downsamples all that it renders itself. But the braille
#: canvases (``ui.mapcanvas``) emit their own truecolor escapes, which pass through Rich
#: without a change. The fold quantizes those to the 16 slots, so that the contract holds
#: for each byte that goes out. The pattern matches any SGR sequence, and it captures its
#: whole parameter list. The quantizer walks the list and does not match a colour that
#: stands alone. Rich writes the foreground and the background of a style as *one*
#: sequence (``ESC[38;2;255;255;255;48;2;0;0;0m``, a QR module). A colour in the middle of
#: such a list is a colour also when other parameters are with it.
_SGR = _re.compile(r"\x1b\[([\d;]*)m")

_SLOT_RGBS: tuple[tuple[int, int, int], ...] = tuple(
    (int(h.lstrip("#")[0:2], 16), int(h.lstrip("#")[2:4], 16), int(h.lstrip("#")[4:6], 16))
    for _, _, h in _VT_SLOTS
)

#: The map from (is_background, r, g, b) to the replacement SGR string. The app uses a few
#: dozen different colours, so this cache stays very small.
_SLOT_CACHE: dict[tuple[bool, int, int, int], str] = {}


def _nearest_slot_params(background: bool, r: int, g: int, b: int) -> str:
    """The 16-colour SGR *parameters* closest to ``(r, g, b)`` in :data:`_VT_SLOTS`.

    Examples are ``22;31``, ``91``, and ``40``. They do not have the ``ESC[…m`` around
    them. Thus the caller can splice the answer into a sequence that also has other
    parameters (:func:`_quantize_sgr`).

    A foreground can be in any slot (30–37 and 90–97). A background can be only in slots
    0–7 (the VT has no bright backgrounds). Thus a bright colour that is used as a fill
    gets the related colour in the dim bank.

    A **foreground in the dim bank states its intent** (``22;3N``, normal intensity), and
    does not emit a bare ``3N``. Bold is brightness on the VT, and ``9N`` is the way that
    the console spells bright. Thus a bare ``31`` immediately after a ``90`` inherits the
    intensity bit, and renders as ``91`` without a message. Quantized art is a run of
    colour spans that are next to each other, and no style boundary between them resets
    the intensity (the bevel of the wordmark has a slate span in every other span, and a
    map raster has neighbouring cells). Thus the promotion happens in the middle of a row
    and gives the wrong colour to half a row. The fourth row of the narrow wordmark (its
    only row with a dim slot) was part red and part light red. The split was at each span
    that followed the bevel.
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
    """The canonical RGB of xterm-256 ``index`` (the cube and the ramps of grey)."""
    if index < 16:
        return _SLOT_RGBS[index]
    if index < 232:
        index -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return (steps[index // 36], steps[index // 6 % 6], steps[index % 6])
    grey = 8 + (index - 232) * 10
    return (grey, grey, grey)


def _quantize_sgr(text: str) -> str:
    """Fold each embedded truecolor or 256-colour SGR down to the 16 palette slots.

    The function walks the parameter list of each sequence (refer to :data:`_SGR`). Thus a
    colour folds when it stands alone, and when it shares its sequence with a second
    colour or an attribute.
    """
    if "8;2;" not in text and "8;5;" not in text:
        return text
    return _SGR.sub(_quantize_params, text)


def _quantize_params(match: _re.Match[str]) -> str:
    """One SGR sequence, in which each ``38/48;2;r;g;b`` and ``38/48;5;n`` is folded to a slot."""
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
    """Compose the full translation table (refer to :data:`_FOLD_TABLE`).

    The table has only what the active font does not have. A character that the font
    draws passes as itself. Thus the Cardputer keeps the accents, the chevrons, and the
    other glyphs that Terminus has and the 512 glyphs of the PicoCalc do not have.
    """
    import unicodedata

    table: dict[int, str] = {}
    # Accented Latin (the base table of the font is Terminus with Cyrillic coverage, with
    # *no* accented Latin at all) and the fullwidth forms. Decompose with NFKD, remove the
    # combining marks, and keep one clean ASCII character, if there is one. é→e, Å→A, ＃→#,
    # ﬁ→(skipped, because it is two characters).
    for first, last in ((0x00A1, 0x024F), (0x1E00, 0x1EFF), (0xFF01, 0xFF5E)):
        for cp in range(first, last + 1):
            if cp in _FOLD_FONT:
                continue
            decomposed = unicodedata.normalize("NFKD", chr(cp))
            base = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
            if len(base) == 1 and base.isascii() and base.isprintable():
                table[cp] = base
    for char, replacement in _FOLD_SINGLES.items():
        if ord(char) not in _FOLD_FONT:
            table[ord(char)] = replacement
    for emoji, compact in _GLYPH_MAP.items():
        if ord(emoji) not in _FOLD_FONT:
            pad = max(0, cell_len(emoji) - cell_len(compact))
            table[ord(emoji)] = compact + " " * pad
    return table


@lru_cache(maxsize=4096)
def _fold_to_font(text: str) -> str:
    """Fold ``text`` down to the inventory of the console font. The cell widths stay the same.

    There are three stages, and the cheapest is first. The first stage is the translation
    table (accents, symbol stand-ins, and the emoji map), in one pass at the C level. The
    second stage is only for text in which a non-ASCII character is still present. It is
    a sweep of each character. It replaces each character that is still not in the font
    with ``?`` at the cell width of the character. The sweep is the safety net. It makes
    the guarantee of the platform, that there are no wide glyphs (refer to
    ``session._has_wide_glyph``), true *by construction*. An emoji that this module does
    not know still leaves as narrow ``?`` characters, and never as a tofu box that breaks
    the cell arithmetic of the frame. The function is cached, because the render output
    repeats much from one frame to the next. When this implementation is bound, the fold
    does not depend on the platform.
    """
    global _FOLD_TABLE
    if _FOLD_TABLE is None:
        _FOLD_TABLE = _build_fold_table()
    folded = (_quantize_sgr(text) if _FOLD_QUANTIZE else text).translate(_FOLD_TABLE)
    if folded.isascii():
        return folded
    if all(ord(ch) in _FOLD_ALLOWED for ch in folded):
        return folded
    return "".join(ch if ord(ch) in _FOLD_ALLOWED else "?" * max(0, cell_len(ch)) for ch in folded)


def fold_text(text: str) -> str:
    """The text filter at the render boundary for the active platform.

    On the regular platform, it returns the text unchanged. On the PicoCalc, it folds each
    string that MeshTerm is about to draw, down to the characters that the console font
    can show (refer to :func:`_fold_to_font`). The strings are names, message bodies, and
    whole rendered ANSI lines. MeshTerm never changes the stored data. ANSI escape
    sequences pass through without a change, because they are pure ASCII and the fold
    never changes ASCII. The function is applied one time, in one central place, in
    :func:`meshterm.ui.tui.render.render_to_ansi`. A screen does not need to call it.

    Args:
        text: The text to fold (or to pass through).

    Returns:
        The folded text. The cell widths stay the same (a wide emoji becomes a glyph and
        padding).
    """
    return _fold_impl(text)


def _no_fold(text: str) -> str:
    """The regular platform: text renders as it is stored."""
    return text


def snr_style(snr: float | None) -> str:
    """Return a theme style name that shows the quality of an SNR value.

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

#: The map from a marker colour to RGB. :func:`mark_rgb` fills it, and a platform switch
#: clears it (the answer is from the *active* theme).
_STYLE_RGB: dict[str, tuple[int, int, int]] = {}


@on_platform
def _bind(platform: Platform) -> None:
    """Bind the choices of the theme that depend on the platform (runs now and at each switch)."""
    global _ACTIVE_THEME, _node_impl, _glyph_impl, _fold_impl, _FOLD_TABLE
    global _FOLD_FONT, _FOLD_ALLOWED, _FOLD_QUANTIZE
    _ACTIVE_THEME = MESH_THEME if platform.truecolor else MESH_THEME_16
    _node_impl = _node_style_spectrum if platform.truecolor else _node_style_quantized
    _glyph_impl = _glyph_identity if platform.emoji else _glyph_compact
    font = FONTS.get(platform.font)
    if font is not None:
        _FOLD_FONT = font
        _FOLD_ALLOWED = font.union(range(0x00, 0x20))
    _FOLD_QUANTIZE = not platform.truecolor
    _fold_impl = _fold_to_font if font is not None else _no_fold
    _STYLE_RGB.clear()
    # MeshTerm computes the emoji pads of the fold table with cell_len when it builds the
    # table. The cell widths can be measured again or patched (the calibration of the emoji
    # width on the regular platform). Thus a platform switch clears the table and the
    # cache, and does not trust old pads.
    _FOLD_TABLE = None
    _fold_to_font.cache_clear()
