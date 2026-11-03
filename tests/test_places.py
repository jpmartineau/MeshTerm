# SPDX-License-Identifier: Apache-2.0
"""The place of a node: the rules, the typed places, the stored places, and the job.

The tiles here are made in the test: each place is a point feature at a known location, so
a test states what the map holds and what the answer must be. No test reads the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshterm.core.geo import TILE_PX, lonlat_to_world
from meshterm.core.models import Observation
from meshterm.core.mvt import GEOM_POINT, Feature, Layer
from meshterm.core.place_store import PlaceStore
from meshterm.core.places import (
    SOURCE_NOMINATIM,
    SOURCE_TILES,
    SOURCE_USER,
    Place,
    moved,
    place_from_nominatim,
    place_from_tiles,
)
from meshterm.core.preferences import Preferences
from meshterm.persistence.repository import Repository
from meshterm.services import places as places_service
from meshterm.services.places import PlaceService

_EXTENT = 4096

#: A point in a city, and the offsets (km) that the tests put places at.
_LAT, _LON = 45.5440, -73.6150
_KM_LAT = 1 / 111.2  # degrees of latitude in one km


def _north(km: float) -> tuple[float, float]:
    """A location ``km`` north of the test point."""
    return _LAT + km * _KM_LAT, _LON


class _Map:
    """Tiles made from named points: each point goes into its tile at each zoom."""

    def __init__(self) -> None:
        self.points: list[tuple[str, str, str, float, float, int]] = []
        self.missing: set[tuple[int, int, int]] = set()
        self.loads: list[tuple[int, int, int]] = []

    def add(self, layer: str, cls: str, name: str, at: tuple[float, float], rank: int = 10) -> None:
        self.points.append((layer, cls, name, at[0], at[1], rank))

    def load(self, z: int, x: int, y: int) -> list[Layer] | None:
        self.loads.append((z, x, y))
        if (z, x, y) in self.missing:
            return None
        layers = {name: Layer(name, _EXTENT) for name in ("place", "mountain_peak")}
        for layer, cls, name, lat, lon, rank in self.points:
            wx, wy = lonlat_to_world(lat, lon, z)
            if int(wx // TILE_PX) != x or int(wy // TILE_PX) != y:
                continue
            px = int((wx / TILE_PX - x) * _EXTENT)
            py = int((wy / TILE_PX - y) * _EXTENT)
            tags = {"class": cls, "name": name, "rank": rank}
            layers[layer].features.append(Feature(GEOM_POINT, [[(px, py)]], tags))
        return list(layers.values())


# -- the rules ----------------------------------------------------------------


def test_a_neighbourhood_gets_the_city_that_holds_it() -> None:
    """A neighbourhood is the fine part, and the city near it is the coarse part."""
    world = _Map()
    world.add("place", "neighbourhood", "Villeray", _north(0.5))
    world.add("place", "city", "Montréal", _north(-6.0), rank=3)
    place = place_from_tiles(_LAT, _LON, world.load)
    assert place == Place("Villeray", "Montréal", SOURCE_TILES)
    assert place.label == "Villeray, Montréal"
    assert place.short == "Villeray"


def test_the_most_specific_area_wins_over_a_nearer_borough() -> None:
    """The most specific area in its radius wins over a nearer borough."""
    world = _Map()
    world.add("place", "suburb", "Villeray–Saint-Michel–Parc-Extension", _north(0.2))
    world.add("place", "neighbourhood", "Villeray", _north(1.0))
    world.add("place", "city", "Montréal", _north(-6.0), rank=3)
    assert place_from_tiles(_LAT, _LON, world.load).fine == "Villeray"


def test_a_large_city_wins_over_a_nearer_small_one() -> None:
    """The rank of a label weighs the distance: a node in Ahuntsic is in Montréal."""
    world = _Map()
    world.add("place", "neighbourhood", "Ahuntsic", _north(0.3))
    world.add("place", "city", "Laval", _north(4.4), rank=11)
    world.add("place", "city", "Montréal", _north(-9.4), rank=3)
    assert place_from_tiles(_LAT, _LON, world.load).coarse == "Montréal"


def test_a_peak_near_the_summit_wins() -> None:
    """A peak within its radius of the node is the place."""
    world = _Map()
    world.add("mountain_peak", "peak", "Mont-Royal", _north(0.2))
    world.add("place", "neighbourhood", "Villeray", _north(0.5))
    world.add("place", "city", "Montréal", _north(-6.0), rank=3)
    place = place_from_tiles(_LAT, _LON, world.load)
    assert place == Place("Mont-Royal", None, SOURCE_TILES, peak=True)


def test_a_peak_past_its_radius_does_not_win() -> None:
    """A peak past its radius does not win over a neighbourhood."""
    world = _Map()
    world.add("mountain_peak", "peak", "Mont-Royal", _north(0.6))
    world.add("place", "neighbourhood", "Villeray", _north(0.5))
    world.add("place", "city", "Montréal", _north(-6.0), rank=3)
    assert place_from_tiles(_LAT, _LON, world.load).fine == "Villeray"


def test_a_village_stands_alone_and_needs_no_fine_tiles() -> None:
    """A village is the place alone, and a node in the country loads only z11 tiles."""
    world = _Map()
    world.add("place", "village", "Val-David", _north(1.5))
    place = place_from_tiles(_LAT, _LON, world.load)
    assert place == Place("Val-David", None, SOURCE_TILES)
    assert {z for z, _, _ in world.loads} == {11}  # the country costs only z11 tiles


def test_a_node_far_from_all_places_gets_the_nearest_settlement() -> None:
    """A node far from all places gets the nearest town, also past the z11 reach."""
    world = _Map()
    world.add("place", "town", "Mont-Laurier", _north(30.0))
    assert place_from_tiles(_LAT, _LON, world.load).fine == "Mont-Laurier"


def test_no_place_near_is_an_empty_answer() -> None:
    """Tiles with no place near give an empty place, which MeshTerm stores."""
    place = place_from_tiles(_LAT, _LON, _Map().load)
    assert place is not None and place.empty
    assert place.label == "" and place.short == ""


def test_a_missing_home_tile_is_no_answer() -> None:
    """When the tile that holds the node has no answer, the result is no answer."""
    world = _Map()
    world.add("place", "village", "Val-David", _north(1.5))
    wx, wy = lonlat_to_world(_LAT, _LON, 11)
    world.missing.add((11, int(wx // TILE_PX), int(wy // TILE_PX)))
    assert place_from_tiles(_LAT, _LON, world.load) is None


def test_a_bad_location_is_no_answer() -> None:
    """A location that is not a real fix gets no place."""
    assert place_from_tiles(0.0, 0.0, _Map().load) is None
    assert place_from_tiles(-97.0, -1042.0, _Map().load) is None


def test_a_label_names_a_city_one_time() -> None:
    """A place whose two parts are the same city shows the name one time."""
    assert Place("Montréal", "Montréal", SOURCE_TILES).label == "Montréal"
    assert Place(None, "Laval", SOURCE_TILES).short == "Laval"


def test_moved_is_past_half_a_km() -> None:
    """A node must move more than half a km before MeshTerm finds its place again."""
    assert not moved(_LAT, _LON, *_north(0.4))
    assert moved(_LAT, _LON, *_north(0.6))


def test_nominatim_gives_fine_and_coarse() -> None:
    """Nominatim gives the neighbourhood and the city."""
    doc = {"address": {"neighbourhood": "Villeray", "suburb": "VSP", "city": "Montréal"}}
    assert place_from_nominatim(doc) == Place("Villeray", "Montréal", SOURCE_NOMINATIM)


def test_nominatim_town_alone() -> None:
    """A Nominatim answer with only a village gives the village alone."""
    doc = {"address": {"village": "Val-David", "county": "Les Laurentides"}}
    assert place_from_nominatim(doc) == Place("Val-David", None, SOURCE_NOMINATIM)


def test_nominatim_error_is_an_empty_answer_and_junk_is_none() -> None:
    """A Nominatim error is an empty answer, and a document that is not an answer is none."""
    assert place_from_nominatim({"error": "Unable to geocode"}).empty
    assert place_from_nominatim(None) is None
    assert place_from_nominatim({"nothing": 1}) is None


# -- the typed places ---------------------------------------------------------


def test_a_typed_place_is_kept_and_cleared(tmp_path: Path) -> None:
    """A typed place stays in the file, and blank text clears it."""
    path = tmp_path / "places.json"
    store = PlaceStore(path)
    store.set("A1B2C3D4E5F6AABB", "Toit du stade", when=100)
    assert PlaceStore(path).get("a1b2c3d4e5f6") == "Toit du stade"
    store.set("a1b2c3d4e5f6", "  ", when=200)
    assert PlaceStore(path).get("a1b2c3d4e5f6") is None


def test_a_field_no_record_knows_is_gone_at_the_next_write(tmp_path: Path) -> None:
    """The store writes from its records, so an unknown field is removed."""
    path = tmp_path / "places.json"
    path.write_text(
        json.dumps({"places": {"a1b2c3d4e5f6": {"name": "X", "set_at": 1, "old": 2}}}),
        encoding="utf-8",
    )
    store = PlaceStore(path)
    store.set("b1b2c3d4e5f6", "Y", when=3)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["places"]["a1b2c3d4e5f6"] == {"name": "X", "set_at": 1}


# -- the stored places --------------------------------------------------------


def _heard(repo: Repository, node: str, lat: float, lon: float, minutes_ago: int) -> None:
    run = repo.start_run("monitor", {}, None)
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    repo.record_observation(run, Observation(node=node, lat=lat, lon=lon, observed_at=when))


def test_node_locations_gives_the_newest(tmp_path: Path) -> None:
    """Each node gets its newest location, and ``since`` reads only newer rows."""
    repo = Repository(tmp_path / "p.db")
    _heard(repo, "aaaaaaaaaaaa", 45.0, -73.0, minutes_ago=30)
    _heard(repo, "aaaaaaaaaaaa", 46.0, -74.0, minutes_ago=5)
    _heard(repo, "bbbbbbbbbbbb", 47.0, -75.0, minutes_ago=10)
    locations = repo.node_locations()
    assert locations["aaaaaaaaaaaa"][:2] == (46.0, -74.0)
    stamp = locations["bbbbbbbbbbbb"][2]
    assert set(repo.node_locations(since=stamp)) == {"aaaaaaaaaaaa"}


def test_a_stored_place_reads_back(tmp_path: Path) -> None:
    """A stored place, also an empty one, reads back the same."""
    repo = Repository(tmp_path / "p.db")
    repo.save_node_place("aaaaaaaaaaaa", 45.0, -73.0, Place("Mont-Royal", None, "tiles", True))
    repo.save_node_place("bbbbbbbbbbbb", 46.0, -74.0, Place(None, None, "nominatim"))
    stored = repo.node_places()
    assert stored["aaaaaaaaaaaa"] == (Place("Mont-Royal", None, "tiles", True), 45.0, -73.0)
    assert stored["bbbbbbbbbbbb"][0].empty


# -- the service --------------------------------------------------------------


def _service(tmp_path: Path, source: str = "tiles_nominatim") -> tuple[PlaceService, Repository]:
    repo = Repository(tmp_path / "p.db")
    ctx = SimpleNamespace(
        repo=repo,
        place_store=PlaceStore(tmp_path / "places.json"),
        preferences=Preferences(values={"place_source": source}),
        log=logging.getLogger("test"),
    )
    return PlaceService(ctx), repo  # type: ignore[arg-type]


def _run(coro):  # noqa: ANN001, ANN202 - a small helper for the async steps
    return asyncio.run(coro)


def test_a_typed_place_wins_and_an_empty_place_is_none(tmp_path: Path) -> None:
    """A typed place wins over a found place, and an empty found place is none."""
    service, repo = _service(tmp_path)
    repo.save_node_place("aaaaaaaaaaaa", 45.0, -73.0, Place("Villeray", "Montréal", "tiles"))
    repo.save_node_place("bbbbbbbbbbbb", 45.0, -73.0, Place(None, None, "tiles"))
    assert service.get("AAAAAAAAAAAA0000").label == "Villeray, Montréal"
    assert service.get("bbbbbbbbbbbb") is None
    before = service.revision
    service.set_typed("aaaaaaaaaaaa", "Le toit")
    assert service.revision > before
    assert service.get("aaaaaaaaaaaa") == Place("Le toit", None, SOURCE_USER)


def test_the_job_uses_the_tiles_then_nominatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job asks the tiles first, then Nominatim one time for each location."""
    service, repo = _service(tmp_path)
    _heard(repo, "aaaaaaaaaaaa", _LAT, _LON, minutes_ago=1)
    world = _Map()
    world.add("place", "neighbourhood", "Ahuntsic", _north(0.3))
    world.add("place", "city", "Laval", _north(4.4), rank=3)
    monkeypatch.setattr(service, "_load_tile", world.load)
    service._scan_after = 0.0
    _run(service.step())
    assert service.get("aaaaaaaaaaaa").label == "Ahuntsic, Laval"
    asked: list[tuple[float, float]] = []

    def nominatim(url: str, lat: float, lon: float) -> object:
        asked.append((lat, lon))
        return {"address": {"neighbourhood": "Ahuntsic", "city": "Montréal"}}

    monkeypatch.setattr(places_service, "ask_nominatim", nominatim)
    _run(service.step())
    assert service.get("aaaaaaaaaaaa") == Place("Ahuntsic", "Montréal", SOURCE_NOMINATIM)
    assert len(asked) == 1
    _run(service.step())
    assert len(asked) == 1  # one answer for each location: never asked again


def test_the_job_finds_the_place_of_an_archived_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archived contact is not on the device, but the store keeps its location."""
    from meshterm.core.contact_store import ContactStore
    from meshterm.core.models import Contact

    service, _repo = _service(tmp_path, source="tiles")
    device = "dd" * 32
    store = ContactStore(tmp_path / "contacts.json")
    gone = Contact(name="Gone", public_key="cc" * 32, lat=_LAT, lon=_LON)
    store.remember_all(device, [gone])
    store.archive(device, gone, when=1_700_000_000)
    service._ctx.contact_store = store
    service._ctx.devstate = SimpleNamespace(
        peek_contacts=lambda: [], peek_self_info=lambda: {"public_key": device}
    )
    world = _Map()
    world.add("place", "village", "Val-David", _north(1.0))
    monkeypatch.setattr(service, "_load_tile", world.load)
    service._scan_after = 0.0
    _run(service.step())
    assert service.get("cc" * 32).fine == "Val-David"


def test_tiles_only_never_asks_nominatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the tiles source, the job never asks Nominatim."""
    service, repo = _service(tmp_path, source="tiles")
    _heard(repo, "aaaaaaaaaaaa", _LAT, _LON, minutes_ago=1)
    world = _Map()
    world.add("place", "village", "Val-David", _north(1.0))
    monkeypatch.setattr(service, "_load_tile", world.load)
    monkeypatch.setattr(places_service, "ask_nominatim", lambda *a: pytest.fail("asked"))
    service._scan_after = 0.0
    _run(service.step())
    _run(service.step())
    assert service.get("aaaaaaaaaaaa").fine == "Val-David"


def test_a_peak_stays_when_nominatim_could_correct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nominatim does not replace a peak that the tiles found."""
    service, repo = _service(tmp_path)
    _heard(repo, "aaaaaaaaaaaa", _LAT, _LON, minutes_ago=1)
    repo.save_node_place("aaaaaaaaaaaa", _LAT, _LON, Place("Mont-Royal", None, "tiles", True))
    monkeypatch.setattr(places_service, "ask_nominatim", lambda *a: pytest.fail("asked"))
    service._scan_after = 0.0
    _run(service.step())
    assert service.get("aaaaaaaaaaaa").peak


def test_a_node_that_moved_is_found_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A node that moved past half a km gets a new place."""
    service, repo = _service(tmp_path, source="tiles")
    repo.save_node_place("aaaaaaaaaaaa", *_north(-5.0), Place("Old", None, "tiles"))
    _heard(repo, "aaaaaaaaaaaa", _LAT, _LON, minutes_ago=1)
    world = _Map()
    world.add("place", "village", "New", _north(1.0))
    monkeypatch.setattr(service, "_load_tile", world.load)
    service._scan_after = 0.0
    _run(service.step())
    assert service.get("aaaaaaaaaaaa").fine == "New"


def test_no_answer_makes_the_node_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A node that got no answer waits before the job asks again."""
    service, repo = _service(tmp_path, source="tiles")
    _heard(repo, "aaaaaaaaaaaa", _LAT, _LON, minutes_ago=1)
    calls: list[int] = []

    def offline(z: int, x: int, y: int) -> None:
        calls.append(z)
        return None

    monkeypatch.setattr(service, "_load_tile", offline)
    service._scan_after = 0.0
    _run(service.step())
    _run(service.step())
    assert len(calls) == 1  # the second step does not ask again at once
    assert service.get("aaaaaaaaaaaa") is None


# -- place mode ---------------------------------------------------------------


def test_place_mode_changes_names_to_places(tmp_path: Path) -> None:
    """Place mode changes node names to places and keeps the key, our end, and hashes."""
    from meshterm.services.trace_runner import make_node_resolver
    from meshterm.ui.pathline import PathHop, path_line
    from meshterm.ui.placemode import PlaceToggle, node_of, placed

    service, repo = _service(tmp_path)
    repo.save_node_place("3daaaaaaaaaa", 45.0, -73.0, Place("Villeray", "Montréal", "tiles"))
    resolve = make_node_resolver(None, {"3daaaaaaaaaa": "VE2XYZ", "a1bbbbbbbbbb": "RAK-1W"})
    line = path_line([None, "3d", "a1", "ff"], resolve)
    hops = placed(line, service, node_of(resolve)).hops
    assert [h.label for h in hops[1:]] == ["Villeray", "(RAK-1W)", "ff"]
    assert hops[1].key == "3d"  # the hop keeps its key, so it keeps its colour
    assert hops[0] == line.hops[0]  # our own end does not change
    assert placed(line, None).hops == line.hops
    lone = PathHop("Bob", key="3daaaaaaaaaa")  # a full key prefix needs no resolver
    assert placed(line.with_hops([lone]), service).hops[0].label == "Villeray"
    toggle = PlaceToggle()
    assert (toggle.hint, toggle.chip) == ("^N places", "Places")
    toggle.flip()
    assert (toggle.hint, toggle.chip) == ("^N names", "Names")
