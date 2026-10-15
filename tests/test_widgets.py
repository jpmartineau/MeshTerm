# SPDX-License-Identifier: Apache-2.0
"""Tests for the helpers of the shared widgets: the standard formatting functions of each screen.

The tests of the host screens test most widgets. The tests here are for the pure text
helpers. Their exact output *is* the convention of the whole app. If these are correct one
time, each caller gets the correct output.
"""

from __future__ import annotations

import pytest
from rich.style import Style
from rich.text import Text

from meshterm.core.models import node_type_label
from meshterm.ui.pathline import (
    PATH_INK,
    POWERLINE_ROUND_CLOSE,
    POWERLINE_ROUND_OPEN,
    POWERLINE_SEP,
    PathHop,
    PathLine,
    _chip_fill,
)
from meshterm.ui.theme import node_style
from meshterm.ui.widgets import (
    DESTINATION,
    ORIGIN,
    format_age,
    format_ago,
    hop_chip,
    mention_chip,
    mention_hop,
    mention_mark,
    name_chip,
    path_text,
    revisit_note,
    route_ends,
    self_chip,
)


def _span_style(text: Text, run: str) -> Style:
    """The parsed style over ``run`` in ``text``. The spans of pathline have style *strings*."""
    return Style.parse(next(str(s.style) for s in text.spans if text.plain[s.start : s.end] == run))


def test_format_age_is_the_bare_column_form() -> None:
    """The lane form has no suffix at each magnitude, for aligned age columns."""
    assert format_age(None) == "never"
    assert format_age(5) == "now"
    assert format_age(90) == "1m"
    assert format_age(7200) == "2h"
    assert format_age(180000) == "2d"
    assert format_age(1300000) == "2w"


def test_format_ago_speaks_grammatical_prose() -> None:
    """The prose form says "5m ago" but bare "now" and "never". It never says "now ago"."""
    assert format_ago(5) == "now"
    assert format_ago(None) == "never"
    assert format_ago(90) == "1m ago"
    assert format_ago(7200) == "2h ago"


def _resolve(hop: str) -> str:
    return {"aa": "Alice", "3d": "Hub"}.get(hop, hop)


def test_path_text_names_hops_and_keeps_hashes_bare() -> None:
    """Named hops read as their name alone, and unnamed hops read as their bare hash."""
    text = path_text(["aa", "77", "3d"], _resolve)
    assert text.plain == "Alice → 77 → Hub"  # no hash in parentheses after a name


def test_path_text_marks_us_white_and_names_in_their_hue() -> None:
    """Our node has the white ``you`` style, and other names have their palette hue."""
    text = path_text(["aa", "3d"], _resolve, self_name="Alice")
    styles = {text.plain[s.start : s.end]: str(s.style) for s in text.spans}
    assert styles.get("Alice") == "you"
    assert "Hub" in styles and styles["Hub"] != "you"


def test_path_text_empty_reads_as_direct() -> None:
    """No hops (or only empty tokens) render the empty word of the caller, muted."""
    assert path_text([], _resolve).plain == "direct"
    assert path_text(["", ""], _resolve).plain == "direct"
    assert path_text([], _resolve, empty="direct — no relays").plain == "direct — no relays"


def test_path_text_greys_an_unnamed_hop_whole() -> None:
    """``path_text`` makes an unnamed hop fully grey.

    A hop that MeshTerm cannot resolve is an unknown node. Its hash reads ``node.unknown``
    from end to end, and the prefix is never lit, because colour marks an identified node.
    This is the rule of the path graph.
    """
    from meshterm.ui.theme import node_style

    text = path_text(["77bb"], _resolve, prefix_bytes=1)
    styles = {text.plain[s.start : s.end]: str(s.style) for s in text.spans}
    assert styles.get("77bb") == "node.unknown"
    assert not any(str(s.style) == node_style("77bb") for s in text.spans)


def test_path_text_hash_as_name_shows_grey_identity_hash() -> None:
    """An unnamed hop shows its own hash at the width of the mode, muted, plus its byte."""
    text = path_text(
        ["e839f2ab"],
        _resolve,
        prefix_bytes=3,
        show_hash=True,
        hash_bytes=1,
        hash_as_name=True,
    )
    assert text.plain == "e839f2 (e8)"  # the identity at the mode width, then the addressed byte
    styles = {text.plain[s.start : s.end]: str(s.style) for s in text.spans}
    assert styles.get("e839f2") == "node.unknown"  # grey: colour is the cue "this is a name"
    from meshterm.ui.theme import node_style

    assert not any(str(s.style) == node_style("e839f2ab") for s in text.spans)  # no prefix lit


def test_path_text_hash_as_name_drops_the_byte_when_it_is_the_whole_hash() -> None:
    """When the mode width is one byte, the identity is the byte, so there is no ``(e8)``."""
    text = path_text(
        ["e839f2ab"],
        _resolve,
        prefix_bytes=1,
        show_hash=True,
        hash_bytes=1,
        hash_as_name=True,
    )
    assert text.plain == "e8"


def test_path_text_trace_flavour_annotates_hashes_and_brackets_us() -> None:
    """show_hash adds the addressed hash to names, and None hops are our device."""
    text = path_text(
        [None, "aa", "77bb", None],
        _resolve,
        self_name="Homestead",
        show_hash=True,
        hash_bytes=1,
        device_hash="c0ffee",
    )
    assert text.plain == "Homestead (c0) → Alice (aa) → 77 → Homestead (c0)"


def test_path_text_dims_the_tail_from_dim_from() -> None:
    """The mirrored return leg (and the arrows into it) renders faint."""
    text = path_text(
        [None, "aa", "3d", "aa", None],
        _resolve,
        self_name="us",
        show_hash=True,
        dim_from=3,
    )
    styles = [(text.plain[s.start : s.end], str(s.style)) for s in text.spans]
    assert ("us", "you") in styles  # the departure keeps the white you
    assert ("us", "faint") in styles  # the return to us is faint
    assert any(run.startswith("Alice") and style == "faint" for run, style in styles)
    assert any(run.startswith("Hub") and style != "faint" for run, style in styles)


# --- the name→key resolver and the rule that a keyless name stays muted --------------


def test_make_name_key_resolver_contacts_win_over_stored_names() -> None:
    """Contacts give the canonical key, stored advert names resolve strangers, misses are None."""
    from meshterm.core.models import Contact
    from meshterm.services.trace_runner import make_name_key_resolver

    contacts = [Contact(name="Alice", public_key="d4" + "0" * 62)]
    stored = {"60aabbccdd11": "Bob", "d40000000000": "Alice"}  # Alice's own stored id
    key_of = make_name_key_resolver(contacts, stored)
    assert key_of("Alice") == "d4" + "0" * 62  # the key of the contact, not the stored id
    assert key_of("alice") == "d4" + "0" * 62  # case-folded
    assert key_of("Bob") == "60aabbccdd11"  # a stranger with a stored name still resolves
    assert key_of("Zed") is None  # no node has the name


def test_a_name_that_two_keys_use_at_the_same_time_resolves_to_no_key() -> None:
    """Two nodes that used one name within the window: MeshTerm cannot tell them apart.

    A channel message carries only the name of its sender. A second key that takes a name
    while the first is still in use must not get the hue, the map, and the page of either.
    """
    from datetime import datetime, timedelta, timezone

    from meshterm.core.models import Contact
    from meshterm.services.trace_runner import make_name_key_resolver

    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    old = Contact(name="LOG.IC 3R", public_key="5d" + "0" * 62, last_seen=now - timedelta(days=3))
    new = Contact(name="LOG.IC 3R", public_key="47" + "0" * 62, last_seen=now)
    assert make_name_key_resolver([old, new], overlap_days=7)("LOG.IC 3R") is None
    assert make_name_key_resolver([new, old], overlap_days=7)("log.ic 3r") is None  # any order

    # A stranger that we only heard is a claim too, and it makes the name of a contact unsure.
    alice = Contact(name="Alice", public_key="d4" + "0" * 62, last_seen=now)
    stranger = {"77ee00112233": "Alice"}
    heard = {"77ee00112233": now - timedelta(hours=1)}
    assert make_name_key_resolver([alice], stranger, heard=heard, overlap_days=7)("Alice") is None
    assert make_name_key_resolver([alice], overlap_days=7)("Alice") == "d4" + "0" * 62


def test_a_name_goes_to_the_newest_key_when_the_old_one_went_quiet() -> None:
    """A new key for a name is usual: a reflash makes a new identity (JP, 2026-10-11).

    An old key that has been silent for longer than the window is the earlier identity of
    the owner. It does not make the name unsure, and the newest key gets the name.
    """
    from datetime import datetime, timedelta, timezone

    from meshterm.core.models import Contact
    from meshterm.services.trace_runner import make_name_key_resolver

    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    smog = Contact(name="Smog", public_key="fa" + "0" * 62, last_seen=now - timedelta(days=1))
    september = {"def5e1786fff": "Smog", "caae7a09503d": "Smog"}
    heard = {"def5e1786fff": now - timedelta(days=21), "caae7a09503d": now - timedelta(days=19)}
    key_of = make_name_key_resolver([smog], september, heard=heard, overlap_days=7)
    assert key_of("Smog") == "fa" + "0" * 62

    # A wider window reaches the old keys again, and the name is unsure.
    wide = make_name_key_resolver([smog], september, heard=heard, overlap_days=30)
    assert wide("Smog") is None

    # The window is measured from the newest key, whichever list it is in.
    renamed = make_name_key_resolver(
        [smog], {"0123456789ab": "Smog"}, heard={"0123456789ab": now}, overlap_days=0
    )
    assert renamed("Smog") == "0123456789ab"


def test_a_key_with_no_time_never_wins_and_no_times_at_all_is_unsure() -> None:
    """A contact that was never heard is not in use. With no time at all, nothing decides."""
    from datetime import datetime, timezone

    from meshterm.core.models import Contact
    from meshterm.services.trace_runner import make_name_key_resolver

    heard = Contact(
        name="Bob", public_key="b0" + "0" * 62, last_seen=datetime(2026, 10, 1, tzinfo=timezone.utc)
    )
    never = Contact(name="Bob", public_key="c0" + "0" * 62)
    assert make_name_key_resolver([never, heard], overlap_days=7)("Bob") == "b0" + "0" * 62
    other = Contact(name="Bob", public_key="d0" + "0" * 62)
    assert make_name_key_resolver([never, other], overlap_days=7)("Bob") is None


def test_the_window_is_the_name_overlap_preference() -> None:
    """With no window given, the resolver reads the preference: a value that a user can change."""
    from meshterm.core.models import NAME_OVERLAP_DAYS
    from meshterm.core.preferences import current

    assert current().name_overlap_days == NAME_OVERLAP_DAYS == 7


def test_name_style_without_a_key_is_the_unknown_grey() -> None:
    """A keyless name has no hue, because colour is only for identities that have a key."""
    from meshterm.ui.theme import name_style, node_style

    assert name_style("Stranger") == "node.unknown"
    keyed = name_style("Alice", "d4" + "0" * 62)
    assert keyed == node_style("d4" + "0" * 62) and keyed.startswith("bold #")


def test_name_rgb_keyless_lands_on_the_muted_grey() -> None:
    """The raster twin: the RGB of a keyless name is the muted grey, and never a crash."""
    from meshterm.ui.widgets import name_rgb

    assert name_rgb("Stranger") == (162, 162, 162)
    assert name_rgb("Alice", "d4" + "0" * 62) != (162, 162, 162)


def test_route_graph_source_label_takes_its_resolved_keys_hue() -> None:
    """The hue of the left endpoint of the graph comes from key_of, or else it is muted."""
    from meshterm.ui.widgets import name_rgb, route_graph_style

    _, _, rgb_known = route_graph_style(
        resolve=lambda h: h,
        self_name="us",
        source="Alice",
        key_of=lambda name: "d4" + "0" * 62 if name == "Alice" else None,
    )
    from meshterm.ui.pathgraph import SRC_NODE

    assert rgb_known(SRC_NODE) == name_rgb("Alice", "d4" + "0" * 62)

    _, _, rgb_unknown = route_graph_style(
        resolve=lambda h: h,
        self_name="us",
        source="Alice",
    )
    assert rgb_unknown(SRC_NODE) == (162, 162, 162)  # an origin that cannot be resolved stays muted


def test_revisit_note_names_the_repeats_and_refuses_to_guess_why() -> None:
    """The one line for a graph that draws a node twice: both readings, and neither is chosen."""
    note = revisit_note(["aa"], _resolve)
    assert note is not None
    assert note.plain == "⚠ Alice repeats — a loop, or two nodes sharing one hash"
    styles = {note.plain[s.start : s.end]: str(s.style) for s in note.spans}
    assert styles.get("⚠ ") == "warn"  # the warn mark of the app, with the theme
    assert styles.get("Alice") not in (None, "warn")  # the name keeps its own node hue


def test_revisit_note_lists_every_repeated_hop() -> None:
    """If more than one hop repeats, the note names them all, joined by commas."""
    note = revisit_note(["aa", "3d"], _resolve)
    assert note is not None
    assert note.plain.startswith("⚠ Alice, Hub repeats — ")
    assert len(note.plain) <= 72  # the budget of the footer and the row of each surface


def test_revisit_note_is_absent_when_nothing_repeats() -> None:
    """If nothing repeats, there is no line. The warning is evidence, and never chrome."""
    assert revisit_note([], _resolve) is None
    assert revisit_note(()) is None


def test_name_chip_is_a_path_line_of_one_hop() -> None:
    """The frame is not an imitation. It *is* the route language, so it cannot differ from it."""
    chip = name_chip("Alice", "a1b2")
    hop = PathLine([PathHop("Alice", key="a1b2")]).text()

    assert chip.plain == hop.plain
    assert [(s.start, s.end, str(s.style)) for s in chip.spans] == [
        (s.start, s.end, str(s.style)) for s in hop.spans
    ]


def test_name_chip_wears_the_hue_as_its_label_padded_like_every_chip(powerline) -> None:
    """The colour scheme of a route: the name has its hue, on a darker fill of that hue."""
    powerline(True)
    chip = name_chip("Alice", "a1b2")

    assert chip.plain.strip(POWERLINE_ROUND_OPEN + POWERLINE_ROUND_CLOSE) == " Alice "
    ink = _span_style(chip, "Alice")
    hue = node_style("a1b2").removeprefix("bold ")
    assert ink.color.name == hue  # the colour of the name on each surface
    assert ink.bgcolor.name == _chip_fill(hue)  # the same hue, darker


def test_name_chip_draws_us_as_the_star_never_as_our_name(powerline) -> None:
    """Our end of a route is the map's own marker, and a sender label is the same."""
    powerline(True)
    chip = name_chip("you", you=True)

    assert "★" in chip.plain and "you" not in chip.plain
    assert _span_style(chip, "★").bgcolor.name == "#404040"  # a neutral grey, not a hue


def test_self_chip_is_white_on_the_fill_of_our_key(powerline) -> None:
    """In a chat, our messages go under ``★ Name``: white on the fill of each known sender.

    The fill is the fill that our hue gives each chip, and the white label tells our chip
    from the other senders, whose label is their hue (JP, 2026-10-14).
    """
    powerline(True)
    ours = self_chip("Me", "5e7f", role=ORIGIN)
    theirs = name_chip("Me", "5e7f", role=ORIGIN)

    assert "★ Me" in ours.plain  # the star in the chip, in the white of the label
    ink, normal = _span_style(ours, "★ Me"), _span_style(theirs, "Me")
    assert ink.bgcolor.name == normal.bgcolor.name
    assert ink.color.name == "#ffffff"


def test_self_chip_is_a_padded_block_without_powerline(powerline) -> None:
    """With no chips, our name is the same pair, padded as a chip is."""
    powerline(True)
    fill = _span_style(self_chip("Me", "5e7f"), "★ Me").bgcolor.name
    powerline(False)
    chip = self_chip("Me", "5e7f")

    assert chip.plain == " ★ Me "
    style = _span_style(chip, " ★ Me ")
    assert (style.color.name, style.bgcolor.name) == ("#ffffff", fill)


def test_a_mention_chip_has_no_at_and_arrow_mode_keeps_it(powerline) -> None:
    """The chip says that a name is a mention. Without a chip, the ``@`` says it."""
    powerline(True)
    chip = mention_chip("Bob", "d4")
    assert "@" not in chip.plain and chip.plain.startswith(POWERLINE_SEP)  # the notch
    assert " Bob " in chip.plain
    powerline(False)
    assert mention_chip("Bob", "d4").plain == "@Bob"


def test_a_mention_of_us_is_white_with_no_star(powerline) -> None:
    """A mention of our name has the white label on the fill of our key, and no star in it."""
    powerline(True)
    ours = mention_chip("Me", "5e7f", you=True)
    theirs = mention_chip("Me", "5e7f")

    assert "★" not in ours.plain
    ink, normal = _span_style(ours, "Me"), _span_style(theirs, "Me")
    assert (ink.color.name, ink.bgcolor.name) == ("#ffffff", normal.bgcolor.name)
    assert not mention_hop("Me", None, you=True).white_ink  # no key: no hue for the fill


@pytest.mark.parametrize("chips", [True, False])
def test_the_mark_of_a_message_that_names_us_is_our_star(powerline, chips) -> None:
    """The white ``★`` of our node, with chips or without them."""
    powerline(chips)
    mark = mention_mark("5e7f")
    assert mark.plain == "★" and str(mark.style) == "you"
    assert str(mention_mark(None).style) == "node.unknown"  # no key: the keyless grey


def test_our_star_is_white_on_the_map() -> None:
    """The map marker of our node is the white of ``you`` (JP, 2026-10-14)."""
    from meshterm.ui.widgets import self_marker

    assert self_marker() == ("★", (0xFF, 0xFF, 0xFF))


def test_self_chip_keeps_the_bare_star_without_our_name_or_key(powerline) -> None:
    """A device that did not report our name or key leaves the star of a route."""
    powerline(True)
    star = name_chip("", you=True).plain
    assert self_chip(None, "5e7f").plain == star
    assert self_chip("Me", None).plain == star


def test_name_chip_greys_a_node_it_cannot_place(powerline) -> None:
    """With no key there is no hue. Only identities that have a key get colour, in a chip too."""
    powerline(True)
    keyless = name_chip("·")

    assert _span_style(keyless, "·").bgcolor.name == "#737373"  # the keyless grey


def test_a_chat_chip_with_no_key_is_black_on_grey(powerline) -> None:
    """In a chat, an unknown sender or mention is black on a grey fill.

    The fill is the grey of plain text, darker by the rule of the chip of a node. A node
    with a key keeps its hue, and arrow mode keeps the ``node.unknown`` grey.
    """
    from meshterm.ui.pathline import _chip_fill
    from meshterm.ui.widgets import author_chip

    powerline(True)
    for chip, label in ((author_chip("Zed", None), "Zed"), (mention_chip("Zed", None), "Zed")):
        style = _span_style(chip, label)
        assert (style.color.name, style.bgcolor.name) == ("#000000", _chip_fill("#cccccc"))
    keyed = _span_style(author_chip("Bob", "d4"), "Bob")
    assert keyed.color.name == node_style("d4").split()[-1]  # a key keeps its hue
    powerline(False)
    assert str(author_chip("Zed", None).spans[0].style) == "node.unknown"


def test_a_chat_chip_with_no_key_is_black_on_the_keyless_fill_on_the_picocalc(
    powerline,
) -> None:
    """The PicoCalc keeps the light grey fill of its keyless chip, with the black label."""
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui.theme import slot_hex
    from meshterm.ui.widgets import author_chip

    powerline(True)
    set_platform(PICOCALC_LYRA)
    try:
        quiet = _span_style(author_chip("Zed", None), "Zed")
        keyless = _span_style(name_chip("Zed"), "Zed")
        assert quiet.bgcolor == keyless.bgcolor
        assert quiet.color.name == slot_hex(0)  # black
    finally:
        set_platform(REGULAR)


def test_name_chip_of_an_origin_closes_on_the_point(powerline) -> None:
    """The author of a message is where a route starts, and the route goes on from it."""
    powerline(True)
    chip = name_chip("Alice", "a1b2", role=ORIGIN)
    hop = PathLine([PathHop("Alice", key="a1b2")], to_destination=False).text()

    assert chip.plain == POWERLINE_ROUND_OPEN + " Alice " + POWERLINE_SEP
    assert [(s.start, s.end, str(s.style)) for s in chip.spans] == [
        (s.start, s.end, str(s.style)) for s in hop.spans
    ]


def test_name_chip_of_a_destination_opens_on_the_notch(powerline) -> None:
    """The node that a mention names is where a route ends, and the route came to it."""
    powerline(True)
    chip = name_chip("@Bob", "c3d4", role=DESTINATION)

    assert chip.plain == POWERLINE_SEP + " @Bob " + POWERLINE_ROUND_CLOSE
    assert _span_style(chip, POWERLINE_SEP).reverse  # the notch, cut out of the fill


def test_name_chip_role_changes_nothing_without_powerline(powerline) -> None:
    """In arrow mode, a lone name has no ends to shape, and a stray arrow reads as a cut route."""
    powerline(False)

    for role in (ORIGIN, DESTINATION):
        assert name_chip("Alice", "a1b2", role=role).plain == "Alice"


def test_a_lone_hop_in_arrow_mode_is_a_name_and_not_a_route(powerline) -> None:
    """Only a route keeps its hues on a highlighted row. A lone name turns white like any name."""
    powerline(False)
    chip = hop_chip(PathHop("Alice", key="a1b2"), role=ORIGIN)

    assert str(chip.style) != PATH_INK
    assert all(str(s.style) != PATH_INK for s in chip.spans)


def test_route_ends_face_each_other_across_one_cell_of_page(powerline) -> None:
    """The middle is not drawn, so no seam joins the two ends. A seam makes them neighbours."""
    powerline(True)
    line = route_ends(PathHop("Alice", key="a1"), PathHop("Bob", key="c3"))

    opening = POWERLINE_ROUND_OPEN + " Alice " + POWERLINE_SEP
    assert line.plain == opening + " " + POWERLINE_SEP + " Bob " + POWERLINE_ROUND_CLOSE


def test_route_ends_are_the_arrow_of_the_app_without_powerline(powerline) -> None:
    """A terminal with no chips reads the two ends as it always did."""
    powerline(False)

    assert route_ends(PathHop("Alice", key="a1"), PathHop("Bob", key="c3")).plain == "Alice → Bob"


def test_name_chip_falls_back_to_the_bare_name_without_powerline(powerline) -> None:
    """If there is no separator glyph to draw a chip, the chip is the bare name.

    The console font of the PicoCalc is one of these cases.
    """
    powerline(False)
    chip = name_chip("Alice", "a1b2")

    assert chip.plain == "Alice"  # the same as a path line in arrow mode makes of one hop
    assert str(next(iter(chip.spans)).style) == node_style("a1b2")  # still its own hue


def test_every_advert_type_has_one_name() -> None:
    """Type 1 is a companion everywhere: the info page, diagnostics, contacts, and legend agree."""
    assert node_type_label(1) == "companion"
    assert node_type_label(2) == "repeater"
    assert node_type_label(3) == "room server"
    assert node_type_label(4) == "sensor"
    assert node_type_label(9) == "type 9"  # an advert type that MeshTerm does not know yet
    assert node_type_label(None) is None


def test_a_compact_tab_strip_is_one_row_and_never_moves_a_label() -> None:
    """Below SHORT_FRAME_BELOW rows the strip draws on its rule, and each tab has a fixed width."""
    from meshterm.ui.widgets import SHORT_FRAME_BELOW, short_frame, tab_strip

    assert short_frame(SHORT_FRAME_BELOW - 1) and not short_frame(SHORT_FRAME_BELOW)
    rows = [tab_strip(["Info", "Routes", "Area"], i, 40, compact=True) for i in range(3)]
    plains = [group.renderables[0].plain for group in rows]
    assert all(len(group.renderables) == 1 for group in rows)
    assert plains[0].startswith("──┤ Info ├") and plains[1].startswith("─── Info ──┤ Routes ├")
    for label in ("Info", "Routes", "Area"):
        assert len({plain.index(label) for plain in plains}) == 1, label
    assert all(len(plain) == 40 for plain in plains)
