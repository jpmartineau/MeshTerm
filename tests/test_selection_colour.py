# SPDX-License-Identifier: Apache-2.0
"""White is what "you picked this" looks like — and nothing else says it.

Three surfaces used to answer the question in the wordmark's teal: the cursor row (fixed
earlier), the chip a dialog's committing button is drawn as, and the heading of the column
a list is sorted by. Teal is a *node* hue as well as the app's identity ink, so every one
of them could collide with the very thing it was highlighting.

What holds now: the cursor row and the active sort column are the ``cursor`` white; a
reverse-video chip is grey, because a chip is chrome and marks where a press lands rather
than claiming a colour; and on the cursor row a node's key-derived hue folds to that same
white, so the highlighted row reads as one thing instead of as a name arguing with its own
selection. Only a *keyed* hue folds — the grey of a node no key could place stays grey.
"""

from __future__ import annotations

import re

import pytest
from rich.text import Text

from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui.contactlist import _SORT_ACTIVE
from meshterm.ui.pathline import PathHop, PathLine
from meshterm.ui.theme import (
    MESH_THEME,
    MESH_THEME_16,
    is_identity_style,
    name_style,
    node_style,
)
from meshterm.ui.tui.render import render_to_ansi

#: A key whose first byte lands well away from white in every direction, so "the hue is
#: gone" is a real observation rather than a coincidence of the palette.
_KEY = "a1b2c3d4e5f6"


@pytest.fixture(autouse=True)
def _regular_platform():
    """Every test states its own platform; default to the desktop and restore after."""
    set_platform(REGULAR)
    yield
    set_platform(REGULAR)


def _row(*, selected: bool) -> Text:
    """A list row in the shape every screen builds: pointer, name, age, key lane."""
    row = Text()
    row.append("❯ " if selected else "  ", style="cursor" if selected else "")
    row.append("Lakeside", style=name_style("Lakeside", _KEY))
    row.append("  ")
    row.append("5m", style="heat.minutes")
    row.append("  ")
    row.append("a1", style=node_style(_KEY))  # the lit half of a key lane
    row.append("b2c3d4e5", style="muted")
    if selected:
        row.style = "cursor"
    return row


def _hues(ansi: str) -> set[str]:
    """The truecolor foregrounds the rendered row actually emitted, as ``#rrggbb``."""
    return {
        "#{:02x}{:02x}{:02x}".format(*tuple(int(part) for part in match.groups()))
        for match in re.finditer(r"\x1b\[[\d;]*?38;2;(\d+);(\d+);(\d+)m", ansi)
    }


# -- the cursor row's names ---------------------------------------------------------


def test_a_selected_row_draws_its_node_name_in_the_cursor_white() -> None:
    """THE ask: the row you are on says "this one", not "this one is mauve"."""
    hue = name_style("Lakeside", _KEY).removeprefix("bold ")

    hues = _hues(render_to_ansi(_row(selected=True), 60))

    assert hue not in hues, f"the identity hue {hue} survived the highlight"
    assert "#ffffff" in hues


def test_an_unselected_row_keeps_every_name_in_its_own_hue() -> None:
    """The fold is the *highlight's* doing; the column below it still reads as identities."""
    hue = name_style("Lakeside", _KEY).removeprefix("bold ")

    assert hue in _hues(render_to_ansi(_row(selected=False), 60))


def test_a_key_lane_s_lit_hash_folds_with_the_name_it_belongs_to() -> None:
    """``highlighted_hash`` lights the hash in the same key-derived hue, so it folds too.

    Otherwise the highlighted row would go white everywhere except one two-character lane
    still shouting its hue — the exact incoherence the fold exists to remove.
    """
    row = Text("  ")
    row.append("a1", style=node_style(_KEY))
    row.append("b2c3d4e5", style="muted")
    row.style = "cursor"

    hues = _hues(render_to_ansi(row, 30))

    assert node_style(_KEY).removeprefix("bold ") not in hues
    assert "#94a3b8" in hues, "the unlit tail of the key is muted and stays muted"


def test_the_highlight_does_not_claim_to_know_an_unidentified_node() -> None:
    """A node no key could place keeps its grey: the selection knows *where* you are, not who.

    ``node.unknown`` is not a hue — it is the absence of one — so folding it to white would
    have the highlighted row assert an identity the app does not have.
    """
    row = Text("  ")
    row.append("3d", style="node.unknown")
    row.style = "cursor"

    assert "#94a3b8" in _hues(render_to_ansi(row, 30))


def test_the_highlight_leaves_every_other_lane_its_own_colour() -> None:
    """Heat, SNR and badges are not identities — the row is highlighted, not repainted."""
    row = Text("  ")
    row.append("5m", style="heat.minutes")
    row.append(" ")
    row.append("+7.5", style="snr.good")
    row.append(" ")
    row.append("stale", style="err")
    row.style = "cursor"

    hues = _hues(render_to_ansi(row, 40))

    assert {"#facc15", "#4ade80", "#f87171"} <= hues


def test_a_path_line_s_chips_keep_their_fills_on_the_cursor_row() -> None:
    """A hue used as a chip's *fill* is the segment, not a name — it is not folded.

    The composed style (``ink on fill``) is deliberately not in the identity vocabulary, so
    a route drawn in chips survives being highlighted intact.
    """
    fill = node_style(_KEY).removeprefix("bold ")
    row = Text("  ")
    row.append(" Lakeside ", style=f"#0f172a on {fill}")
    row.style = "cursor"

    assert f"48;2;{int(fill[1:3], 16)};" in render_to_ansi(row, 40)


def test_a_path_line_keeps_every_hop_s_hue_on_the_cursor_row() -> None:
    """A route's hues are content: they are how one hop is told from the next.

    The chip form was never folded (its fills are outside the vocabulary), so an
    arrow-drawn route — every path line on the PicoCalc, and on any terminal without the
    powerline glyphs — has to survive the same way, or the same picked row says two
    different things on the two platforms. The widget stamps its own extent and the fold
    spares what lies inside it.
    """
    hops = [PathHop("Lakeside", key=_KEY), PathHop("Waymarker", key="77" * 6)]
    row = Text("❯ ", style="cursor")
    row.append_text(PathLine(hops, mode="plain").text())
    row.style = "cursor"

    hues = _hues(render_to_ansi(row, 60))

    assert node_style(_KEY).removeprefix("bold ") in hues
    assert node_style("77").removeprefix("bold ") in hues


def test_a_name_beside_a_path_line_still_folds() -> None:
    """The exception is the route, not the row: a name lane next to one is still a name."""
    row = Text("  ")
    row.append("Lakeside", style=name_style("Lakeside", _KEY))
    row.append("  ")
    row.append_text(PathLine([PathHop("Waymarker", key="77" * 6)], mode="plain").text())
    row.style = "cursor"

    hues = _hues(render_to_ansi(row, 60))

    assert name_style("Lakeside", _KEY).removeprefix("bold ") not in hues  # the lane folded
    assert node_style("77").removeprefix("bold ") in hues  # …the route did not


@pytest.mark.parametrize("platform", [REGULAR, PICOCALC_LYRA])
def test_the_message_paths_dialog_colours_its_picked_route_like_its_graph(platform) -> None:  # noqa: ANN001
    """The end JP asked for: the picked row and the graph above it agree on each node.

    The graph labels a relay with a marker and one byte of hash — the colour is what says
    *which node* — so a row whitened hop by hop left the reader nothing to match them by.
    """
    set_platform(platform)
    from datetime import timedelta

    from meshterm.core.models import ChatMessage, utcnow
    from meshterm.services.message_paths import Arrival
    from meshterm.ui.message_paths_screen import MessagePathsScreen

    now = utcnow()
    arrivals = [
        Arrival(when=now, hops=("3d63", "a1b2"), snr=4.0),
        Arrival(when=now + timedelta(seconds=2), hops=("a1b2",), snr=-2.0),
    ]
    screen = MessagePathsScreen(
        ChatMessage(text="on my way", is_channel=True, created_at=now),
        arrivals,
        matched=True,
        resolve=lambda hop: {"3d63": "Hilltop-Repeater", "a1b2": "Waymarker"}.get(hop, hop),
        prefix_bytes=1,
        self_name="Homestead",
        summary="heard twice",
        source="Alice",
    )
    picked = next(line for line in screen.render_body(72) if "❯" in line)

    for hop in ("3d63", "a1b2"):
        assert _ink(node_style(hop)) in picked, f"the picked route lost {hop}'s hue"


def _ink(style: str) -> str:
    """The escape a style emits before its text, as it appears on the bound platform."""
    return render_to_ansi(Text("x", style=style), 3).split("x")[0]


def test_the_fold_never_touches_the_row_it_was_handed() -> None:
    """Rendered Texts are cache keys, so the rewrite has to happen on a copy.

    A mutated input would poison the ANSI cache: the next lookup would hash the *folded*
    row and miss, and any caller re-rendering the same object unselected would get white.
    """
    row = _row(selected=True)
    before = [(span.start, span.end, str(span.style)) for span in row.spans]

    render_to_ansi(row, 60)

    assert [(s.start, s.end, str(s.style)) for s in row.spans] == before


def test_the_ansi_cache_tells_a_highlighted_row_from_the_same_row_unhighlighted() -> None:
    """Same words, same spans, different base style — and the cache keys on the base."""
    plain_ansi = render_to_ansi(_row(selected=False), 60)
    lit_ansi = render_to_ansi(_row(selected=True), 60)

    assert plain_ansi != lit_ansi
    assert render_to_ansi(_row(selected=False), 60) == plain_ansi  # not clobbered


@pytest.mark.parametrize("platform", [REGULAR, PICOCALC_LYRA])
def test_the_fold_speaks_whichever_hue_vocabulary_is_bound(platform) -> None:  # noqa: ANN001
    """PicoCalc quantizes the hue to a palette slot; the rule above it is the same rule."""
    set_platform(platform)
    row = Text("  ")
    row.append("Lakeside", style=name_style("Lakeside", _KEY))
    row.style = "cursor"

    lit = render_to_ansi(row, 30)
    unlit = render_to_ansi(Text("  Lakeside"), 30)

    assert lit != unlit
    assert "Lakeside" in re.sub(r"\x1b\[[\d;]*m", "", lit)
    if platform is PICOCALC_LYRA:
        assert "\x1b[1;97mLakeside" in lit, lit  # slot 15, the console's white
    else:
        assert "38;2;255;255;255mLakeside" in lit, lit


# -- the vocabulary itself ------------------------------------------------------------


@pytest.mark.parametrize("platform", [REGULAR, PICOCALC_LYRA])
def test_every_hue_a_key_can_mint_is_recognised_as_one(platform) -> None:  # noqa: ANN001
    """:func:`node_style` read backwards has to answer for all 256 first bytes.

    A hue the vocabulary missed would be a name that quietly refused to fold on exactly
    the nodes whose keys start with that byte.
    """
    set_platform(platform)

    assert all(is_identity_style(node_style(f"{byte:02x}")) for byte in range(256))


@pytest.mark.parametrize(
    "style",
    [
        "you",
        "cursor",
        "muted",
        "node.unknown",
        "brand",
        "heat.now",
        "err",
        "bold #ffffff",
        "#94a3b8",
        "",
        "none",
    ],
)
def test_nothing_but_a_key_derived_hue_counts_as_an_identity(style: str) -> None:
    """Everything else on a row means something the highlight has no claim on."""
    assert not is_identity_style(style)


def test_the_two_platforms_hue_vocabularies_cannot_be_confused() -> None:
    """Both are in the set at once, so they must not overlap or a switch would misfold.

    The spectrum tops out at ``0xf2`` (value 0.95) and every console slot spells ``0xff``,
    which is what keeps the answer independent of the platform bound at the time.
    """
    set_platform(REGULAR)
    spectrum = {node_style(f"{byte:02x}") for byte in range(256)}
    set_platform(PICOCALC_LYRA)
    slots = {node_style(f"{byte:02x}") for byte in range(256)}

    assert not (spectrum & slots)


# -- the reverse-video chip -----------------------------------------------------------


def test_the_selection_chip_is_grey_on_both_platforms() -> None:
    """A dialog button, an Abort, the arrow-mode composer slot: chrome, not a hue."""
    for theme in (MESH_THEME, MESH_THEME_16):
        chip = theme.styles["selected"]
        assert chip.reverse, "the chip is reverse video — the page's own ink shows through"
        assert chip.color is not None
        red, green, blue = chip.color.get_truecolor()
        saturation = (max(red, green, blue) - min(red, green, blue)) / max(red, green, blue)
        assert saturation <= 0.25, (
            f"the chip's fill {chip.color!r} carries a hue; it should be grey "
            "(the palette's greys are slate, so a little blue cast is the family)"
        )


def test_the_chip_is_light_enough_for_the_page_s_ink_to_read_on_it() -> None:
    """Reverse video puts the terminal's background *in* the chip, so the fill carries it."""
    red, green, blue = MESH_THEME.styles["selected"].color.get_truecolor()

    assert min(red, green, blue) >= 0x80


def test_the_console_chip_uses_a_background_the_vt_can_actually_address() -> None:
    """The VT has no bright backgrounds, and slot 8's dark grey reverses to black."""
    chip = MESH_THEME_16.styles["selected"]

    assert chip.color.number == 7, "slot 7 is the only grey a reverse can put behind text"
    assert chip.bold is False, "a dim-slot style states its intent about bold"


def test_no_theme_style_still_paints_a_selection_in_the_wordmark_s_teal() -> None:
    """The regression this whole file is named for, pinned as a property of the theme."""
    for theme in (MESH_THEME, MESH_THEME_16):
        for name in ("selected", "cursor"):
            assert theme.styles[name].color.get_truecolor() != (
                MESH_THEME.styles["brand"].color.get_truecolor()
            ), name


# -- the active sort column -----------------------------------------------------------


def test_the_active_sort_column_is_lit_in_the_cursor_white() -> None:
    """The same claim as the cursor row, one axis over — so the same ink.

    It used to be a bare ``#22d3ee``, which put a *selection* cue back on the node
    spectrum and, on the console, straight onto the wordmark's own slot.
    """
    from meshterm.ui.contactlist import ContactsSort
    from meshterm.ui.widgets import _sort_header

    lit = _sort_header("NAME", "name", ContactsSort("name", True))
    idle = _sort_header("HEARD", "heard", ContactsSort("name", True))

    assert lit == "[cursor]NAME ▲[/]"
    assert idle == "HEARD"
    assert _SORT_ACTIVE == "cursor", "the live list and the static table light it the same"


def test_the_sort_cue_survives_the_console_s_sixteen_slots() -> None:
    """A bare hex would have been quantized at the fold; a theme name is chosen deliberately."""
    set_platform(PICOCALC_LYRA)
    header = Text("NAME ▲", style=_SORT_ACTIVE)

    assert "\x1b[1;97m" in render_to_ansi(header, 20)  # slot 15, not the brand's 14
