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
