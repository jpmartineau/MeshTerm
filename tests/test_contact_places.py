# SPDX-License-Identifier: Apache-2.0
"""Tests for the place of a node on the Contacts list, the CLI, and the map filter.

The place is the named area nearest the location of a node (refer to
:mod:`meshterm.core.places`). These tests use a small stand-in for the place service, so
that no database and no network are necessary.
"""

from __future__ import annotations

import re

from rich.cells import cell_len

from meshterm.core.models import Contact
from meshterm.core.places import SOURCE_TILES, Place
from meshterm.ui.contactlist import (
    CONTACTS_SORT_COLUMNS,
    CONTACTS_SORT_OPENS_ASCENDING,
    PLACE_LANE,
    ContactListScreen,
    ContactRow,
)
from meshterm.ui.contacts_screen import ContactsScreen
from meshterm.ui.widgets import ContactsSort

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_ALICE = "aa" * 32
_BOB = "bb" * 32
_CARL = "dd" * 32
_US = "cc" * 32


class _Places:
    """A stand-in for :class:`~meshterm.services.places.PlaceService`: ``get`` and ``revision``."""

    def __init__(self, found: dict[str, Place]) -> None:
        self.found = found
        self.revision = 0

    def get(self, node: str | None) -> Place | None:
        return self.found.get((node or "")[:12])


def _places() -> _Places:
    return _Places(
        {
            _ALICE[:12]: Place("Villeray", "Montréal", SOURCE_TILES),
            _BOB[:12]: Place("Mont Royal", None, SOURCE_TILES, peak=True),
        }
    )


def _sort(name: str = "name") -> ContactsSort:
    return ContactsSort.from_name(name, CONTACTS_SORT_COLUMNS, CONTACTS_SORT_OPENS_ASCENDING)


def _screen(places: _Places | None = None, sort: str = "name") -> ContactsScreen:
    contacts = [
        Contact(name="Alice", public_key=_ALICE),
        Contact(name="Bob", public_key=_BOB),
        Contact(name="Carl", public_key=_CARL),
    ]
    return ContactsScreen(
        "Homestead", _US, contacts, 1, {}, _sort(sort), places=places if places else _places()
    )


def _lines(screen: ContactListScreen, width: int) -> list[str]:
    return [_ANSI.sub("", line) for line in screen.render_body(width)]


def _row(screen: ContactListScreen, width: int, name: str) -> str:
    return next(line for line in _lines(screen, width) if name in line)


def _names(screen: ContactListScreen, width: int = 200) -> list[str]:
    """The contact names in the order that the list draws them (our node first)."""
    drawn = _lines(screen, width)
    order = ["Homestead", "Alice", "Bob", "Carl"]
    return sorted(order, key=lambda n: next(i for i, line in enumerate(drawn) if n in line))


# -- part 1: the PLACE column ---------------------------------------------------------


def test_place_column_is_immediately_before_the_key() -> None:
    """The header reads NAME, HEARD, PKTS, PLACE, KEY in that order."""
    header = _lines(_screen(), 200)[0]
    labels = [header.index(label) for label in ("NAME", "HEARD", "PKTS", "PLACE", "KEY")]
    assert labels == sorted(labels)
    assert "PLACE" in header and header.rstrip().endswith("KEY")


def test_place_shows_the_full_label_and_a_dash_when_unknown() -> None:
    """A known place is its label. An unknown place is the faint dash of the other lanes."""
    screen = _screen()
    alice = _row(screen, 200, "Alice")
    carl = _row(screen, 200, "Carl")
    assert "Villeray, Montréal" in alice
    assert "Mont Royal" in _row(screen, 200, "Bob")
    # The place is before the key on the row, and a node with no place has the dash there.
    assert alice.index("Villeray") < alice.index("aaaa")
    assert carl.index("—", carl.index("Carl") + 4) < carl.index("dddd")
    assert "Villeray" not in carl


def test_place_column_cuts_no_value() -> None:
    """The label of a row keeps its full place and its full key, however narrow the terminal."""
    screen = _screen()
    label = "".join(
        _ANSI.sub("", str(getattr(c, "label", "")))
        for c in screen._choices()  # noqa: SLF001
    )
    assert "Villeray, Montréal" in label
    assert _ALICE in label
    assert _US in label


def test_pan_keeps_header_and_rows_aligned() -> None:
    """After the table pans, the PLACE label is still over the places of the rows."""
    screen = _screen()
    width = 53
    for _ in range(3):
        lines = _lines(screen, width)
        header = lines[0]
        alice = next(line for line in lines if "Alice" in line)
        if "PLACE" in header and "Villeray" in alice:
            assert header.index("PLACE") == alice.index("Villeray")
            break
        screen.handle("right")
    else:  # pragma: no cover - the place must come into view
        raise AssertionError("the PLACE lane never came into view")


def test_pan_holds_the_name_still() -> None:
    """The mark, the glyph, and the name stay while the lanes slide."""
    screen = _screen()
    before = _row(screen, 53, "Alice")[:12]
    screen.handle("right")
    screen.handle("right")
    assert _row(screen, 53, "Alice")[:12] == before


def test_unselected_rows_are_cut_only_at_the_screen_edge() -> None:
    """A narrow terminal shows a cut mark at the edge. The row itself is not shortened."""
    screen = _screen()
    narrow = _row(screen, 40, "Alice")
    assert cell_len(narrow) <= 40
    wide = _row(screen, 200, "Alice")
    assert "Villeray, Montréal" in wide and _ALICE in wide


def test_scroll_atom_is_in_the_hint_only_while_the_row_overflows() -> None:
    """The footer names ←→ exactly when the highlighted row runs past the edge."""
    screen = _screen()
    screen.render_body(40)
    assert "←→ scroll" in screen.footer_hint
    screen.render_body(400)
    assert "←→ scroll" not in screen.footer_hint


def test_our_node_shows_its_place() -> None:
    """The row of our node shows its place when MeshTerm knows it, and a dash if not."""
    places = _places()
    assert "—" in _row(_screen(places), 200, "Homestead").split("(you)")[1]
    places.found[_US[:12]] = Place("Rosemont", "Montréal", SOURCE_TILES)
    assert "Rosemont, Montréal" in _row(_screen(places), 200, "Homestead")


def test_place_is_sortable() -> None:
    """The sort ring has a place column. Places run A to Z, and a node with none runs last."""
    screen = _screen(sort="place")
    assert [PLACE_LANE.column] == [c for c in CONTACTS_SORT_COLUMNS if c == "place"]
    # Mont Royal (Bob), then Villeray (Alice), then no place (Carl). Our node stays first.
    assert _names(screen) == ["Homestead", "Bob", "Alice", "Carl"]
    screen.handle("ctrl_down")
    assert _names(screen) == ["Homestead", "Carl", "Alice", "Bob"]


def test_ctrl_arrows_reach_the_place_column() -> None:
    """Ctrl+→ goes name, heard, packets, place, hash."""
    screen = _screen()
    seen = []
    for _ in range(4):
        screen.handle("ctrl_right")
        seen.append(screen._sort.column)  # noqa: SLF001
    assert seen == ["heard", "packets", "place", "hash"]


def test_a_found_place_draws_without_a_new_screen() -> None:
    """When the service counts a change, the list reads the places again and keeps the highlight."""
    places = _places()
    screen = _screen(places)
    screen.handle("down")
    screen.handle("down")
    current = screen._current_choice().value  # noqa: SLF001
    assert "Verdun" not in "\n".join(_lines(screen, 200))
    places.found[_CARL[:12]] = Place("Verdun", "Montréal", SOURCE_TILES)
    places.revision += 1
    assert "Verdun, Montréal" in _row(screen, 200, "Carl")
    assert screen._current_choice().value == current  # noqa: SLF001


def test_lists_without_the_lane_are_unchanged() -> None:
    """A list that does not declare the lane draws no PLACE column."""
    screen = ContactListScreen(
        "Pick",
        rows=[ContactRow(value=1, name="Alice", key=_ALICE, place="Villeray, Montréal")],
        prefix_bytes=1,
        sort=ContactsSort.from_name("name"),
    )
    assert "PLACE" not in _lines(screen, 100)[0]
    assert "Villeray" not in _row(screen, 100, "Alice")


# -- part 2: the filter ---------------------------------------------------------------


def _type(screen: ContactListScreen, text: str) -> None:
    for char in text:
        screen.handle("text", char)


def test_filter_matches_the_place() -> None:
    """Typed text narrows the list by the place of a node, in any case."""
    screen = _screen()
    _type(screen, "villeray")
    shown = "\n".join(_lines(screen, 200))
    assert "Alice" in shown
    assert "Bob" not in shown and "Carl" not in shown


def test_filter_matches_the_coarse_part_of_the_place() -> None:
    """The city part of a place matches too, and a name still matches as before."""
    screen = _screen()
    _type(screen, "montr")
    shown = "\n".join(_lines(screen, 200))
    assert "Alice" in shown and "Bob" not in shown
    screen.handle("escape")
    _type(screen, "bob")
    shown = "\n".join(_lines(screen, 200))
    assert "Bob" in shown and "Alice" not in shown


# -- part 3: the CLI and the machine object -------------------------------------------


def _cli_contacts(place_of):  # noqa: ANN001, ANN202 - a lookup function or None
    from meshterm.tools.contacts import _listing

    contacts = [
        Contact(name="Alice", public_key=_ALICE, key_prefix=_ALICE[:12]),
        Contact(name="Carl", public_key=_CARL, key_prefix=_CARL[:12]),
    ]
    return _listing(contacts, {}, 1, place_of)


def _render(face, listing):  # noqa: ANN001, ANN202 - the renderer class
    import io

    from meshterm.ui import script

    buffer = io.StringIO()
    face(script.console(buffer)).render((listing,))
    return buffer.getvalue()


def test_node_object_has_a_place_key_that_is_null_when_unknown() -> None:
    """The machine object of a node has six keys. ``place`` is the label, or ``null``."""
    import json

    from meshterm.ui.renderers import JsonRenderer

    docs = json.loads(_render(JsonRenderer, _cli_contacts(_places().get)))
    alice, carl = (d["node"] for d in docs)
    assert list(alice) == ["name", "key", "hash", "type", "place", "self"]
    assert alice["place"] == "Villeray, Montréal"
    assert carl["place"] is None


def test_contacts_without_a_lookup_gives_null_places() -> None:
    """A listing built with no place lookup still has the key, with ``null``."""
    import json

    from meshterm.ui.renderers import JsonRenderer

    docs = json.loads(_render(JsonRenderer, _cli_contacts(None)))
    assert [d["node"]["place"] for d in docs] == [None, None]


def test_plain_face_has_a_place_column_before_key() -> None:
    """The PLACE column sits just before KEY, shows the label, and uses ``-`` when unknown."""
    from meshterm.ui.renderers import PlainRenderer

    header, alice, carl = _render(PlainRenderer, _cli_contacts(_places().get)).splitlines()
    labels = header.split()
    assert labels.index("PLACE") == labels.index("KEY") - 1
    assert "Villeray, Montréal" in alice
    assert alice.index("Villeray") < alice.index(_ALICE)
    assert carl.split()[-2] == "-" and carl.split()[-1] == _CARL


# -- part 4: the map filter -----------------------------------------------------------


def test_marker_matches_its_label_or_its_place() -> None:
    """The find filter of the map matches the label or the place, in any case."""
    from meshterm.core.models import MapMarker

    marker = MapMarker("Alice", 45.5, -73.6, place="Villeray, Montréal")
    assert marker.matches("")
    assert marker.matches("ali")
    assert marker.matches("villeray")
    assert marker.matches("montréal")
    assert not marker.matches("verdun")
    assert not MapMarker("Alice", 45.5, -73.6).matches("villeray")


def test_with_places_stamps_a_copy_of_each_marker() -> None:
    """``with_places`` reads the place of each key, and does not change the given markers."""
    from meshterm.core.models import MapMarker
    from meshterm.ui.map_screen import with_places

    original = [
        MapMarker("Alice", 45.5, -73.6, key=_ALICE),
        MapMarker("Carl", 45.6, -73.7, key=_CARL),
        MapMarker("Nokey", 45.7, -73.8),
    ]
    stamped = with_places(original, _places().get)
    assert [m.place for m in stamped] == ["Villeray, Montréal", "", ""]
    assert [m.place for m in original] == ["", "", ""]
    assert [m.label for m in stamped] == ["Alice", "Carl", "Nokey"]


def test_map_find_matches_a_place() -> None:
    """Typed text narrows the map to the nodes whose place matches it."""
    from meshterm.core.models import MapMarker
    from meshterm.ui.map_screen import MapScreen, with_places
    from tests.test_map import _StubSession, _StubSource

    markers = with_places(
        [
            MapMarker("Alice", 45.5, -73.6, key=_ALICE),
            MapMarker("Bob", 45.6, -73.7, key=_BOB),
            MapMarker("Carl", 45.7, -73.8, key=_CARL),
        ],
        _places().get,
    )
    screen = MapScreen(_StubSession(80, 24), markers, _StubSource(), 14)
    screen.render_body(80)  # the first paint makes the viewport that a key needs
    for char in "villeray":
        screen.handle("text", char)
    assert [m.label for m in screen._matches()] == ["Alice"]  # noqa: SLF001
