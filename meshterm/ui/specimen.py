# SPDX-License-Identifier: Apache-2.0
"""The visual-language specimen: all the marks, icons, colours, and folds on one screen.

``meshterm specimen`` prints this card through the real functions of the platform: the
active theme, the ``glyph()`` function that all icons go through, the fold at the render
boundary, and the heat and SNR scales. Thus what the card shows is what the platform
draws, not a model of it. On the PicoCalc console, the card is also the acceptance card
for the font and the palette (all nine P3 marks, the node glyphs, braille, and the 16
programmed slots). On a desktop terminal, it shows the same content with the emoji and
the truecolor of the regular platform. The card fits the limits of the PicoCalc: each
line is 53 cells or less, and the full card is 24 rows or less.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text

from ..platforms import get_platform
from .packet_viewer import KIND_ICONS, PAYLOAD_ICONS
from .theme import _VT_SLOTS, fold_text, glyph, name_style, snr_style
from .tui.fkeys import FPair, active_deck
from .widgets import _recency_style, channel_glyph, name_chip

#: Demonstration identities for the row of name colours: ``(name, key)``. The keys are
#: synthetic. We chose them so that their first bytes are in three different sectors of
#: the hue wheel. On the console, these are three different palette slots (refer to
#: ``theme._NODE_SLOT_HEXES``).
_DEMO_NAMES = (("Alice", "a1b2"), ("Lakeside", "3d63"), ("Lounge", "cc10"))


def _palette_row() -> Text:
    """The 16 slots as filled blocks: first the dim bank, then the bright bank.

    On the PicoCalc, the blocks use the real slots of the console (``color(N)``). Thus the
    row shows exactly what ``setvtrgb`` programmed. On the regular platform, the blocks
    use the RGB values of the design instead. They are a preview of the same palette in
    truecolor.
    """
    row = Text()
    for slot, _, hex_ in _VT_SLOTS:
        style = f"color({slot})" if not get_platform().truecolor else hex_
        row.append("██", style=style)
    return row


def _ages_row() -> Text:
    """The heat scale at its own anchors: one age a little inside each of the seven steps."""
    row = Text()
    anchors = (
        ("now", 0),
        ("20m", 1200),
        ("3h", 10800),
        ("2d", 172800),
        ("2w", 1209600),
        ("3mo", 7776000),
        ("never", None),
    )
    for label, secs in anchors:
        if row.plain:
            row.append(" ")
        row.append(label, style=_recency_style(secs))
    return row


def specimen_lines() -> list[RenderableType]:
    """The specimen card, with one renderable for each line.

    Print the lines through the themed console.
    """
    platform = get_platform()
    lines: list[RenderableType] = []
    lines.append(Text.assemble(("Specimen", "accent"), ("  ·  ", "muted"), platform.name))
    lines.append(Text())
    lines.append(_palette_row())
    legend = Text("stock palette · 8 muted · 9-14 node hues · 15 you", style="muted")
    lines.append(legend)
    lines.append(Text())

    status = Text("status  ")
    for mark, style in (("✓", "ok"), ("✗", "err"), ("⚠", "warn"), ("●", "err"), ("○", "muted")):
        status.append(mark + " ", style=style)
    lines.append(status)

    # The marks of the node types, in the theme's own ``type.*`` entries. The app draws
    # each node that has a type in these same styles (ui.widgets.NODE_GLYPHS). Thus this
    # row is the marker language itself, not a copy of it. The star and the unknown ring
    # have no type of their own. They use the ``you`` white and the ``muted`` grey, the
    # same as everywhere else in the app.
    nodes = Text("nodes   ")
    nodes.append("★ ", style="you")
    for mark, style in (
        ("● ", "type.node"),
        ("▲ ", "type.repeater"),
        ("■ ", "type.room"),
        ("◉ ", "type.sensor"),
        ("○", "muted"),
    ):
        nodes.append(mark, style=style)
    lines.append(nodes)

    classes = Text("classes ")
    for icon in KIND_ICONS.values():
        classes.append(glyph(icon) + " ", style="brand")
    classes.append("  payloads ", style="muted")
    for typename in ("REQ", "RESPONSE", "TEXT_MSG", "GRP_TXT", "GRP_DATA", "ANON_REQ"):
        classes.append(glyph(PAYLOAD_ICONS[typename]) + " ")
    for typename in ("PATH", "TRACE", "MULTIPART", "CONTROL"):
        classes.append(glyph(PAYLOAD_ICONS[typename]) + " ")
    lines.append(classes)

    channels = Text("channels ")
    channels.append(channel_glyph("#public", None) + " name-derived  ", style="brand")
    channels.append(glyph("🌐") + " public  ")
    channels.append(glyph("🔒") + " private")
    lines.append(channels)

    concepts = Text("concepts ")
    for icon in ("📡", "🕒", "🔄", "💾", "📂", "🔑", "🗑", "✎", "⚡", "⭐", "📤", "📨"):
        concepts.append(glyph(icon) + " ")
    for icon in ("🔔", "🔕", "📱", "🔗", "🏆", "⌨", "📖", "💰"):
        concepts.append(glyph(icon) + " ")
    lines.append(concepts)

    lines.append(Text("keys    ⌫ ⇧ … ↕ ↔ ↻ ⌖ ❯ ▸"))
    lines.append(Text())

    names = Text("names   ")
    for name, key in _DEMO_NAMES:
        names.append(name + "  ", style=name_style(name, key))
    names.append("me", style="you")
    lines.append(names)

    # The same names as chips. These are the sender labels of the chat. The path line's
    # own segment draws them, so this row is also a one-hop sample of the route language:
    # the hue in the fill, the star for our end, and the keyless grey for a node that
    # nobody can identify.
    chips = Text("chips   ")
    for name, key in _DEMO_NAMES[:2]:
        chips.append_text(name_chip(name, key))
        chips.append(" ")
    chips.append_text(name_chip("", you=True))
    chips.append(" ")
    chips.append_text(name_chip("·"))
    lines.append(chips)

    lines.append(Text("heard   ").append_text(_ages_row()))

    snr = Text("snr     ")
    for reading in (8.0, -2.0, -12.0):
        snr.append(f"{reading:+.1f} ", style=snr_style(reading))
    lines.append(snr)

    # What the render boundary does to stored text. On the PicoCalc, accents fold, because
    # the console font has no accented Latin letters. There, the raw form shows as tofu.
    # Cyrillic is in the font, so it passes through on both platforms.
    folded = fold_text("café Montréal")
    lines.append(Text.assemble("text    ", (folded, "brand"), ("  ·  ", "muted"), "привет мир"))
    lines.append(Text())

    chart = Text("chart   ")
    chart.append("⡀⡄⡆⣆⣦⣶⣷⣿", style="snr.good")
    chart.append("⣶⣤⣀", style="snr.ok")
    chart.append("⡀⠄⠂", style="snr.bad")
    chart.append("  braille", style="muted")
    lines.append(chart)

    # The F-key lane, on a platform that draws one: both fills, a dim slot, and an
    # unassigned slot. Thus the card shows each state that a chip can have. The platform's
    # own deck builds the lane, because this is the acceptance screen, and it must use the
    # real renderer.
    deck = active_deck()
    if deck is not None:
        lane = (
            FPair("Region", "home"),
            FPair("You", "locate", enabled=False),
            None,
            FPair("Page ↓", "pagedown", "Bottom", "end"),
            FPair("Page ↑", "pageup", "Top", "home"),
        )
        lines.append(Text())
        lines.append(deck.lane_text(lane))
        lines.append(deck.lane_text(lane, shifted=True))
    return lines
