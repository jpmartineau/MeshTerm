# SPDX-License-Identifier: Apache-2.0
"""White shows "you selected this". Nothing else in the app shows it.

Three surfaces once answered this question in the teal of the wordmark: the highlighted row
(we corrected this one earlier), the chip that is the committing button of a dialog, and
the heading of the column that sorts a list. Teal is a *node* hue and also the identity
colour of the app. Thus each of the three surfaces could have the same colour as the thing
that it highlighted.

These rules apply now:

* The highlighted row and the active sort column are the ``cursor`` white.
* A reverse-video chip is grey. A chip is chrome, and it shows where a key press acts. It
  does not claim a colour.
* On the highlighted row, the hue of a node (it comes from the key) folds to the same
  white. Thus the highlighted row looks like one thing. A name does not conflict with its
  own highlight.
* Only a *keyed* hue folds. The grey of a node that no key can place stays grey.
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

#: A key whose first byte gives a hue that is far from white in each direction. Thus "the
#: hue is gone" is a real observation and not a coincidence of the palette.
_KEY = "a1b2c3d4e5f6"


@pytest.fixture(autouse=True)
def _regular_platform():
    """Each test sets its own platform.

    The default is the desktop, and the fixture restores it after the test.
    """
    set_platform(REGULAR)
    yield
    set_platform(REGULAR)


def _row(*, selected: bool) -> Text:
    """A list row in the form that each screen builds: pointer, name, age, key lane."""
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
    """The truecolor foregrounds that the rendered row emitted, as ``#rrggbb``."""
    return {
        "#{:02x}{:02x}{:02x}".format(*tuple(int(part) for part in match.groups()))
        for match in re.finditer(r"\x1b\[[\d;]*?38;2;(\d+);(\d+);(\d+)m", ansi)
    }


# -- the names on the highlighted row -----------------------------------------------


def test_a_selected_row_draws_its_node_name_in_the_cursor_white() -> None:
    """The row that you are on shows "this one", not "this one is mauve"."""
    hue = name_style("Lakeside", _KEY).removeprefix("bold ")

    hues = _hues(render_to_ansi(_row(selected=True), 60))

    assert hue not in hues, f"the identity hue {hue} survived the highlight"
    assert "#ffffff" in hues


def test_an_unselected_row_keeps_every_name_in_its_own_hue() -> None:
    """Only the *highlight* causes the fold. The rows below it still show identities."""
    hue = name_style("Lakeside", _KEY).removeprefix("bold ")

    assert hue in _hues(render_to_ansi(_row(selected=False), 60))


def test_a_key_lane_s_lit_hash_folds_with_the_name_it_belongs_to() -> None:
    """``highlighted_hash`` lights the hash in the same key-derived hue, so it folds too.

    If it did not fold, the highlighted row would be white everywhere except one lane of two
    characters that keeps its hue. The fold exists to remove this incoherence.
    """
    row = Text("  ")
    row.append("a1", style=node_style(_KEY))
    row.append("b2c3d4e5", style="muted")
    row.style = "cursor"

    hues = _hues(render_to_ansi(row, 30))

    assert node_style(_KEY).removeprefix("bold ") not in hues
    assert "#94a3b8" in hues, "the unlit tail of the key is muted and stays muted"


def test_the_highlight_does_not_claim_to_know_an_unidentified_node() -> None:
    """A node that no key can place keeps its grey. The highlight knows *where* you are, not who.

    ``node.unknown`` is not a hue. It is the absence of a hue. If the fold changed it to
    white, the highlighted row would claim an identity that the app does not have.
    """
    row = Text("  ")
    row.append("3d", style="node.unknown")
    row.style = "cursor"

    assert "#94a3b8" in _hues(render_to_ansi(row, 30))


def test_the_highlight_leaves_every_other_lane_its_own_colour() -> None:
    """Heat, SNR, and badges are not identities.

    The row is highlighted, and the paint of its lanes does not change.
    """
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
    """A hue that is the *fill* of a chip is the segment, not a name. It does not fold.

    The composed style (``ink on fill``) is not in the identity vocabulary, and this is
    intended. Thus a route that is drawn in chips stays complete when it is highlighted.
    """
    fill = node_style(_KEY).removeprefix("bold ")
    row = Text("  ")
    row.append(" Lakeside ", style=f"#0f172a on {fill}")
    row.style = "cursor"

    assert f"48;2;{int(fill[1:3], 16)};" in render_to_ansi(row, 40)


def test_a_path_line_keeps_every_hop_s_hue_on_the_cursor_row() -> None:
    """The hues of a route are content: they show the difference between one hop and the next.

    The fold never changed the chip form (its fills are not in the vocabulary). An
    arrow-drawn route must keep its hues in the same way. An arrow-drawn route is each path
    line on the PicoCalc, and on each terminal without the powerline glyphs. If it did not
    keep its hues, the same selected row would show two different things on the two
    platforms. The widget stamps its own extent, and the fold does not change the text in
    that extent.
    """
    hops = [PathHop("Lakeside", key=_KEY), PathHop("Waymarker", key="77" * 6)]
    row = Text("❯ ", style="cursor")
    row.append_text(PathLine(hops, mode="plain").text())
    row.style = "cursor"

    hues = _hues(render_to_ansi(row, 60))

    assert node_style(_KEY).removeprefix("bold ") in hues
    assert node_style("77").removeprefix("bold ") in hues


def test_a_name_beside_a_path_line_still_folds() -> None:
    """The exception is the route, not the row. A name lane next to a route is still a name."""
    row = Text("  ")
    row.append("Lakeside", style=name_style("Lakeside", _KEY))
    row.append("  ")
    row.append_text(PathLine([PathHop("Waymarker", key="77" * 6)], mode="plain").text())
    row.style = "cursor"

    hues = _hues(render_to_ansi(row, 60))

    assert name_style("Lakeside", _KEY).removeprefix("bold ") not in hues  # the lane folded
    assert node_style("77").removeprefix("bold ") in hues  # the route did not fold


@pytest.mark.parametrize("platform", [REGULAR, PICOCALC_LYRA])
def test_the_message_paths_dialog_colours_its_picked_route_like_its_graph(platform) -> None:  # noqa: ANN001
    """The result that JP asked for: the selected row and the graph above it agree on each node.

    The graph labels a relay with a marker and one byte of hash. The colour shows *which
    node* it is. If the row became white hop by hop, the user could not match the row with
    the graph.
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
    """The escape sequence that a style emits before its text, on the platform that is bound."""
    return render_to_ansi(Text("x", style=style), 3).split("x")[0]


def test_the_fold_never_touches_the_row_it_was_handed() -> None:
    """The rendered Texts are cache keys, so the rewrite must be on a copy.

    If the fold changed its input, the ANSI cache would have wrong data. The next lookup
    would hash the *folded* row and find no entry. A caller that renders the same object
    again, not selected, would get white.
    """
    row = _row(selected=True)
    before = [(span.start, span.end, str(span.style)) for span in row.spans]

    render_to_ansi(row, 60)

    assert [(s.start, s.end, str(s.style)) for s in row.spans] == before


def test_the_ansi_cache_tells_a_highlighted_row_from_the_same_row_unhighlighted() -> None:
    """The words and the spans are the same, but the base style is different.

    The cache keys on the base.
    """
    plain_ansi = render_to_ansi(_row(selected=False), 60)
    lit_ansi = render_to_ansi(_row(selected=True), 60)

    assert plain_ansi != lit_ansi
    assert render_to_ansi(_row(selected=False), 60) == plain_ansi  # not clobbered


@pytest.mark.parametrize("platform", [REGULAR, PICOCALC_LYRA])
def test_the_fold_speaks_whichever_hue_vocabulary_is_bound(platform) -> None:  # noqa: ANN001
    """The PicoCalc quantizes the hue to a palette slot. The rule above it is the same rule."""
    set_platform(platform)
    row = Text("  ")
    row.append("Lakeside", style=name_style("Lakeside", _KEY))
    row.style = "cursor"

    lit = render_to_ansi(row, 30)
    unlit = render_to_ansi(Text("  Lakeside"), 30)

    assert lit != unlit
    assert "Lakeside" in re.sub(r"\x1b\[[\d;]*m", "", lit)
    if platform is PICOCALC_LYRA:
        assert "\x1b[1;97mLakeside" in lit, lit  # slot 15, the white of the console
    else:
        assert "38;2;255;255;255mLakeside" in lit, lit


# -- the vocabulary -------------------------------------------------------------------


@pytest.mark.parametrize("platform", [REGULAR, PICOCALC_LYRA])
def test_every_hue_a_key_can_mint_is_recognised_as_one(platform) -> None:  # noqa: ANN001
    """The reverse of :func:`node_style` must recognize each of the 256 first bytes.

    Suppose the vocabulary does not include a hue. Then a name does not fold, without a
    message, on exactly the nodes whose keys start with that byte.
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
    """Each other style on a row has a meaning that the highlight must not change."""
    assert not is_identity_style(style)


def test_the_two_platforms_hue_vocabularies_cannot_be_confused() -> None:
    """The hue vocabularies of the two platforms must not overlap.

    Both are in the set at the same time. If they overlapped, a platform switch would fold a
    wrong style. The spectrum has a maximum of ``0xf2`` (value 0.95), and each console slot
    is ``0xff``. Thus the answer does not depend on the platform that is bound at the time.
    """
    set_platform(REGULAR)
    spectrum = {node_style(f"{byte:02x}") for byte in range(256)}
    set_platform(PICOCALC_LYRA)
    slots = {node_style(f"{byte:02x}") for byte in range(256)}

    assert not (spectrum & slots)


# -- the reverse-video chip -----------------------------------------------------------


def test_the_selection_chip_is_grey_on_both_platforms() -> None:
    """A dialog button, an Abort, and the arrow-mode composer slot are chrome. They have no hue."""
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
    """Reverse video puts the background of the terminal *in* the chip, so the fill has it."""
    red, green, blue = MESH_THEME.styles["selected"].color.get_truecolor()

    assert min(red, green, blue) >= 0x80


def test_the_console_chip_uses_a_background_the_vt_can_actually_address() -> None:
    """The VT has no bright backgrounds, and the dark grey of slot 8 reverses to black."""
    chip = MESH_THEME_16.styles["selected"]

    assert chip.color.number == 7, "slot 7 is the only grey a reverse can put behind text"
    assert chip.bold is False, "a dim-slot style states its intent about bold"


def test_no_theme_style_still_paints_a_selection_in_the_wordmark_s_teal() -> None:
    """The regression that gives this file its name, as a property of the theme."""
    for theme in (MESH_THEME, MESH_THEME_16):
        for name in ("selected", "cursor"):
            assert theme.styles[name].color.get_truecolor() != (
                MESH_THEME.styles["brand"].color.get_truecolor()
            ), name


# -- the active sort column -----------------------------------------------------------


def test_the_active_sort_column_is_lit_in_the_cursor_white() -> None:
    """The active sort column makes the same claim as the highlighted row.

    The claim is on another axis, so the ink is the same. It was once a bare ``#22d3ee``.
    This put a *selection* cue on the node spectrum again. On the console, it put the cue on
    the slot of the wordmark.
    """
    from meshterm.ui.contactlist import ContactsSort
    from meshterm.ui.widgets import _sort_header

    lit = _sort_header("NAME", "name", ContactsSort("name", True))
    idle = _sort_header("HEARD", "heard", ContactsSort("name", True))

    assert lit == "[cursor]NAME ▲[/]"
    assert idle == "HEARD"
    assert _SORT_ACTIVE == "cursor", "the live list and the static table light it the same"


def test_the_sort_cue_survives_the_console_s_sixteen_slots() -> None:
    """The fold would quantize a bare hex colour. A theme name is a deliberate choice."""
    set_platform(PICOCALC_LYRA)
    header = Text("NAME ▲", style=_SORT_ACTIVE)

    assert "\x1b[1;97m" in render_to_ansi(header, 20)  # slot 15, not slot 14 of the brand
