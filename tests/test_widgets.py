# SPDX-License-Identifier: Apache-2.0
"""Shared-widget helper tests: the canonical formatting primitives every screen leans on.

Most widgets are exercised through their host screens' tests; what lives here are the
pure text helpers whose exact output *is* the app-wide convention — get these right once
and every caller inherits it.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

from meshterm.core.models import node_type_label
from meshterm.ui.pathline import (
    POWERLINE_ROUND_CLOSE,
    POWERLINE_ROUND_OPEN,
    PathHop,
    PathLine,
)
from meshterm.ui.theme import node_style
from meshterm.ui.widgets import (
    format_age,
    format_ago,
    name_chip,
    path_text,
    revisit_note,
)


def _span_style(text: Text, run: str) -> Style:
    """The style over ``run`` in ``text``, parsed — pathline spans carry style *strings*."""
    return Style.parse(next(str(s.style) for s in text.spans if text.plain[s.start : s.end] == run))


def test_format_age_is_the_bare_column_form() -> None:
    """The lane form stays suffix-free at every magnitude, for aligned age columns."""
    assert format_age(None) == "never"
    assert format_age(5) == "now"
    assert format_age(90) == "1m"
    assert format_age(7200) == "2h"
    assert format_age(180000) == "2d"
    assert format_age(1300000) == "2w"


def test_format_ago_speaks_grammatical_prose() -> None:
    """The prose form says "5m ago" but bare "now"/"never" — never "now ago"."""
    assert format_ago(5) == "now"
    assert format_ago(None) == "never"
    assert format_ago(90) == "1m ago"
    assert format_ago(7200) == "2h ago"


def _resolve(hop: str) -> str:
    return {"aa": "Alice", "3d": "Hub"}.get(hop, hop)


def test_path_text_names_hops_and_keeps_hashes_bare() -> None:
    """Named hops read as their name alone; unnamed ones as their bare hash."""
    text = path_text(["aa", "77", "3d"], _resolve)
    assert text.plain == "Alice → 77 → Hub"  # no parenthesized hash after a name


def test_path_text_marks_us_white_and_names_in_their_hue() -> None:
    """Our own node takes the white ``you`` style; other names their palette hue."""
    text = path_text(["aa", "3d"], _resolve, self_name="Alice")
    styles = {text.plain[s.start : s.end]: str(s.style) for s in text.spans}
    assert styles.get("Alice") == "you"
    assert "Hub" in styles and styles["Hub"] != "you"


def test_path_text_empty_reads_as_direct() -> None:
    """No hops (or only empty tokens) renders the caller's empty word, muted."""
    assert path_text([], _resolve).plain == "direct"
    assert path_text(["", ""], _resolve).plain == "direct"
    assert path_text([], _resolve, empty="direct — no relays").plain == "direct — no relays"


def test_path_text_greys_an_unnamed_hop_whole() -> None:
    """``path_text`` greys an unnamed hop whole.

    An unresolvable hop is an unknown node: its hash reads ``node.unknown`` end to
    end, the prefix never lit, because colour marks an identified node — the path
    graph's rule.
    """
    from meshterm.ui.theme import node_style

    text = path_text(["77bb"], _resolve, prefix_bytes=1)
    styles = {text.plain[s.start : s.end]: str(s.style) for s in text.spans}
    assert styles.get("77bb") == "node.unknown"
    assert not any(str(s.style) == node_style("77bb") for s in text.spans)


def test_path_text_hash_as_name_shows_grey_identity_hash() -> None:
    """An unnamed hop stands in its own hash at the mode width, muted, plus its byte."""
    text = path_text(
        ["e839f2ab"],
        _resolve,
        prefix_bytes=3,
        show_hash=True,
        hash_bytes=1,
        hash_as_name=True,
    )
    assert text.plain == "e839f2 (e8)"  # mode width identity, then the addressed byte
    styles = {text.plain[s.start : s.end]: str(s.style) for s in text.spans}
    assert styles.get("e839f2") == "node.unknown"  # grey — colour is the "this is a name" cue
    from meshterm.ui.theme import node_style

    assert not any(str(s.style) == node_style("e839f2ab") for s in text.spans)  # no prefix lit


def test_path_text_hash_as_name_drops_the_byte_when_it_is_the_whole_hash() -> None:
    """When the mode width is one byte, the identity already is the byte — no ``(e8)``."""
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
    """show_hash appends the addressed hash to names; None hops are our device."""
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
    """The mirrored return leg (and the arrows into it) render faint."""
    text = path_text(
        [None, "aa", "3d", "aa", None],
        _resolve,
        self_name="us",
        show_hash=True,
        dim_from=3,
    )
    styles = [(text.plain[s.start : s.end], str(s.style)) for s in text.spans]
    assert ("us", "you") in styles  # the departure keeps the white you
    assert ("us", "faint") in styles  # the landing back on us is faint
    assert any(run.startswith("Alice") and style == "faint" for run, style in styles)
    assert any(run.startswith("Hub") and style != "faint" for run, style in styles)


# --- the name→key resolver and the keyless-stays-muted rule --------------------------


def test_make_name_key_resolver_contacts_win_over_stored_names() -> None:
    """Contacts resolve first; stored advert names fill in strangers; misses are None."""
    from meshterm.core.models import Contact
    from meshterm.services.trace_runner import make_name_key_resolver

    contacts = [Contact(name="Alice", public_key="d4" + "0" * 62)]
    stored = {"60aabbccdd11": "Bob", "77ee00112233": "Alice"}  # a stale stored Alice
    key_of = make_name_key_resolver(contacts, stored)
    assert key_of("Alice") == "d4" + "0" * 62  # the contact's key, not the stored id
    assert key_of("alice") == "d4" + "0" * 62  # casefolded
    assert key_of("Bob") == "60aabbccdd11"  # a stored-name stranger still lands
    assert key_of("Zed") is None  # nobody carries the name


def test_name_style_without_a_key_is_the_unknown_grey() -> None:
    """A keyless name has no hue — colour is reserved for keyed identities."""
    from meshterm.ui.theme import name_style, node_style

    assert name_style("Stranger") == "node.unknown"
    keyed = name_style("Alice", "d4" + "0" * 62)
    assert keyed == node_style("d4" + "0" * 62) and keyed.startswith("bold #")


def test_name_rgb_keyless_lands_on_the_muted_grey() -> None:
    """The raster twin: a keyless name's RGB is the muted grey, never a crash."""
    from meshterm.ui.widgets import name_rgb

    assert name_rgb("Stranger") == (148, 163, 184)
    assert name_rgb("Alice", "d4" + "0" * 62) != (148, 163, 184)


def test_route_graph_source_label_takes_its_resolved_keys_hue() -> None:
    """The graph's left endpoint hue rides key_of: resolved → the key's hue, else muted."""
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
    assert rgb_unknown(SRC_NODE) == (148, 163, 184)  # unresolvable origin stays muted


def test_revisit_note_names_the_repeats_and_refuses_to_guess_why() -> None:
    """The one line a graph drawing a node twice owes its reader — both readings, neither picked."""
    note = revisit_note(["aa"], _resolve)
    assert note is not None
    assert note.plain == "⚠ Alice repeats — a loop, or two nodes sharing one hash"
    styles = {note.plain[s.start : s.end]: str(s.style) for s in note.spans}
    assert styles.get("⚠ ") == "warn"  # the app's warn mark, themed
    assert styles.get("Alice") not in (None, "warn")  # the name keeps its own node hue


def test_revisit_note_lists_every_repeated_hop() -> None:
    """More than one hop repeated names them all, comma-joined, still one sentence."""
    note = revisit_note(["aa", "3d"], _resolve)
    assert note is not None
    assert note.plain.startswith("⚠ Alice, Hub repeats — ")
    assert len(note.plain) <= 72  # the footer/row budget every surface renders it inside


def test_revisit_note_is_absent_when_nothing_repeats() -> None:
    """No repeats, no line — the warning is evidence, never chrome."""
    assert revisit_note([], _resolve) is None
    assert revisit_note(()) is None


def test_name_chip_is_a_path_line_of_one_hop() -> None:
    """The framing isn't imitated — it *is* the route language, so it can't drift from it."""
    chip = name_chip("Alice", "a1b2")
    hop = PathLine([PathHop("Alice", key="a1b2")]).text()

    assert chip.plain == hop.plain
    assert [(s.start, s.end, str(s.style)) for s in chip.spans] == [
        (s.start, s.end, str(s.style)) for s in hop.spans
    ]


def test_name_chip_wears_the_hue_as_its_fill_padded_like_every_chip(powerline) -> None:
    """A route's colour scheme: the node's hue fills the chip, the ink on it is dark."""
    powerline(True)
    chip = name_chip("Alice", "a1b2")

    assert chip.plain.strip(POWERLINE_ROUND_OPEN + POWERLINE_ROUND_CLOSE) == " Alice "
    ink = _span_style(chip, "Alice")
    assert ink.bgcolor.name == node_style("a1b2").removeprefix("bold ")  # the hue
    assert ink.color.name == "#0f172a"  # the chip ink every hop is lettered in


def test_name_chip_draws_us_as_the_star_never_as_our_name(powerline) -> None:
    """Our end of a route is the map's own marker; a sender label is no different."""
    powerline(True)
    chip = name_chip("you", you=True)

    assert "★" in chip.plain and "you" not in chip.plain
    assert _span_style(chip, "★").bgcolor.name == "#3f3f46"  # neutral grey, not a hue


def test_name_chip_greys_a_node_it_cannot_place(powerline) -> None:
    """No key, no hue — colour is reserved for keyed identities, in a chip as in a route."""
    powerline(True)
    keyless = name_chip("·")

    assert _span_style(keyless, "·").bgcolor.name == "#64748b"  # the keyless grey


def test_name_chip_falls_back_to_the_bare_name_without_powerline(powerline) -> None:
    """No separator glyph to draw a chip with — the PicoCalc's console font among them."""
    powerline(False)
    chip = name_chip("Alice", "a1b2")

    assert chip.plain == "Alice"  # exactly what an arrow-mode path line makes of one hop
    assert str(next(iter(chip.spans)).style) == node_style("a1b2")  # still its own hue


def test_every_advert_type_has_one_name() -> None:
    """Type 1 is a companion everywhere: info, diagnostics, contacts and the legend agree."""
    assert node_type_label(1) == "companion"
    assert node_type_label(2) == "repeater"
    assert node_type_label(3) == "room server"
    assert node_type_label(4) == "sensor"
    assert node_type_label(9) == "type 9"  # an advert type MeshTerm does not know yet
    assert node_type_label(None) is None


def test_a_compact_tab_strip_is_one_row_and_never_moves_a_label() -> None:
    """Below COMPACT_TABS_BELOW rows the strip draws on its rule, every tab a fixed width."""
    from meshterm.ui.widgets import COMPACT_TABS_BELOW, tab_compact, tab_strip

    assert tab_compact(COMPACT_TABS_BELOW - 1) and not tab_compact(COMPACT_TABS_BELOW)
    rows = [tab_strip(["Info", "Routes", "Area"], i, 40, compact=True) for i in range(3)]
    plains = [group.renderables[0].plain for group in rows]
    assert all(len(group.renderables) == 1 for group in rows)
    assert plains[0].startswith("──┤ Info ├") and plains[1].startswith("─── Info ──┤ Routes ├")
    for label in ("Info", "Routes", "Area"):
        assert len({plain.index(label) for plain in plains}) == 1, label
    assert all(len(plain) == 40 for plain in plains)
