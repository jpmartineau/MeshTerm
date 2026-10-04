# SPDX-License-Identifier: Apache-2.0
"""The visual-language specimen: every mark, icon, colour and fold on one screen.

``meshterm specimen`` prints this through the platform's real machinery — the active
theme, the ``glyph()`` icon funnel, the render-boundary fold, the heat and SNR scales —
so what it shows *is* what the platform draws, not a mock-up of it. On the PicoCalc
console it doubles as the font/palette acceptance card (all nine P3 marks, the node
glyphs, braille, the 16 programmed slots); on a desktop terminal it shows the same
content in the regular platform's emoji-and-truecolor dress. Sized to the PicoCalc
floor: every line ≤ 53 cells, the whole card ≤ 24 rows.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text

from ..platforms import get_platform
from .packet_viewer import KIND_ICONS, PAYLOAD_ICONS
from .theme import _VT_SLOTS, fold_text, glyph, name_style, snr_style
from .tui.fkeys import FPair, active_deck
from .widgets import _recency_style, channel_glyph, name_chip

#: Demo identities for the name-colour row: ``(name, key)``. The keys are synthetic, and
#: chosen so their first bytes land in three different sectors of the hue wheel — on the
#: console that means three different palette slots (see ``theme._NODE_SLOT_HEXES``).
_DEMO_NAMES = (("Alice", "a1b2"), ("Lakeside", "3d63"), ("Lounge", "cc10"))


def _palette_row() -> Text:
    """The 16 slots as filled blocks, dim bank then bright bank.

    On PicoCalc the blocks address the console's real slots (``color(N)``), so the row
    shows exactly what ``setvtrgb`` programmed; on the regular platform they carry the
    design's RGB values instead — a preview of the same palette in truecolor.
    """
    row = Text()
    for slot, _, hex_ in _VT_SLOTS:
        style = f"color({slot})" if not get_platform().truecolor else hex_
        row.append("██", style=style)
    return row


def _ages_row() -> Text:
    """The heat scale over its own anchors — one age just inside each of the seven steps."""
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
    """The specimen card, one renderable per line (print through the themed console)."""
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

    # The node-type marks in the theme's own ``type.*`` entries — the same styles every
    # typed node in the app is drawn in (ui.widgets.NODE_GLYPHS), so this row *is* the
    # marker language rather than a restatement of it. The star and the unknown ring have
    # no type of their own: they take the ``you`` white and ``muted`` grey they carry
    # everywhere else.
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

    # The same names as chips — the chat's sender labels, drawn through the path line's
    # own segment, so this row is also a one-hop sample of the route language: hue in the
    # fill, our end the star, a node nobody can place in the keyless grey.
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

    # What the render boundary makes of stored text: accents fold on PicoCalc (the raw
    # form would be tofu there — the console font has no accented Latin), Cyrillic is
    # font-native and passes through on both platforms.
    folded = fold_text("café Montréal")
    lines.append(Text.assemble("text    ", (folded, "brand"), ("  ·  ", "muted"), "привет мир"))
    lines.append(Text())

    chart = Text("chart   ")
    chart.append("⡀⡄⡆⣆⣦⣶⣷⣿", style="snr.good")
    chart.append("⣶⣤⣀", style="snr.ok")
    chart.append("⡀⠄⠂", style="snr.bad")
    chart.append("  braille", style="muted")
    lines.append(chart)

    # The F-key lane, on a platform that draws one: both fills, plus a dim slot and an
    # unassigned one, so the card shows every state a chip can be in. Built through the
    # platform's own deck — this is the acceptance screen, so it must be the real renderer.
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
