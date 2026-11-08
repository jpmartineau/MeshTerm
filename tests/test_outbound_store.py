# SPDX-License-Identifier: Apache-2.0
"""Tests for the store of outbound paths: sites, the active site, fixed devices, the lookup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshterm.core.outbound_store import (
    SOURCE_DEVICE,
    SOURCE_SITE,
    OutboundChoice,
    OutboundStore,
    clean_path,
)

DEVICE = "c0ffee" * 10 + "abcd"  # a public key of 32 bytes
HOME = [("3d63c6429436",), ("27d4396a2967", "f2c24f54551e")]
LAB = [("a100008dc55e",)]


@pytest.fixture
def store(tmp_path: Path) -> OutboundStore:
    """An empty store in a temporary directory."""
    return OutboundStore(tmp_path / "outbound.json")


def test_clean_path_takes_a_list_or_a_spec_and_refuses_junk() -> None:
    """A path is whole bytes of hex, at least one hop, and no node two times."""
    assert clean_path(["27D4", " f2c2 "]) == ("27d4", "f2c2")
    assert clean_path("27d4,f2c2") == ("27d4", "f2c2")
    assert clean_path([]) is None
    assert clean_path(["27d"]) is None  # half a byte
    assert clean_path(["zz"]) is None
    assert clean_path(["27d4", "27d4"]) is None  # a node two times is never useful


def test_an_empty_store_applies_no_list(store: OutboundStore) -> None:
    """With no site and no fixed device, the lookup gives nothing."""
    assert store.sites() == []
    assert store.active_site() is None
    assert store.lookup(DEVICE) == OutboundChoice()


def test_the_active_site_applies_to_each_device(store: OutboundStore) -> None:
    """The active site is the answer for each device that is not fixed."""
    store.put_site("Home", HOME)
    store.put_site("Foulab", LAB)
    store.set_active("foulab")  # names match without regard to case
    assert store.lookup(DEVICE) == OutboundChoice(tuple(LAB), SOURCE_SITE, "Foulab")
    assert store.lookup(None).name == "Foulab"  # an unknown device still gets the site


def test_a_fixed_device_keeps_its_own_list_whatever_site_is_active(store: OutboundStore) -> None:
    """A fixed device wins over the active site, also with an empty list."""
    store.put_site("Home", HOME)
    store.set_active("Home")
    store.fix_device(DEVICE.upper(), "uConsole", LAB)
    assert store.lookup(DEVICE) == OutboundChoice(tuple(LAB), SOURCE_DEVICE, "uConsole")
    assert store.lookup("ab" * 32).source == SOURCE_SITE  # another device follows the site
    store.fix_device(DEVICE, "uConsole", [])
    assert store.lookup(DEVICE) == OutboundChoice((), SOURCE_DEVICE, "uConsole")
    assert store.unfix_device(DEVICE)
    assert store.lookup(DEVICE).source == SOURCE_SITE
    assert not store.unfix_device(DEVICE)


def test_site_names_are_unique_and_a_rename_keeps_the_site_active(store: OutboundStore) -> None:
    """Two sites cannot share a name, and an active site stays active when it is renamed."""
    store.put_site("Home", HOME)
    store.put_site("Work", LAB)
    store.set_active("Home")
    with pytest.raises(ValueError, match="already exists"):
        store.put_site("WORK", HOME, old_name="Home")  # a rename onto another site
    store.put_site("Maison", HOME, old_name="home")
    assert [site.name for site in store.sites()] == ["Maison", "Work"]
    assert store.active_site().name == "Maison"
    with pytest.raises(ValueError):
        store.put_site("  ", HOME)
    with pytest.raises(KeyError):
        store.put_site("School", HOME, old_name="Nowhere")
    with pytest.raises(KeyError):
        store.set_active("Nowhere")


def test_a_put_with_the_same_name_replaces_the_paths(store: OutboundStore) -> None:
    """A put on a name that exists replaces that site, and removes duplicate paths."""
    store.put_site("Home", HOME)
    store.put_site("HOME", [*LAB, *LAB])
    assert [(site.name, site.paths) for site in store.sites()] == [("HOME", LAB)]


def test_removing_the_active_site_leaves_no_site_active(store: OutboundStore) -> None:
    """A deleted site cannot stay active."""
    store.put_site("Home", HOME)
    store.set_active("Home")
    assert store.remove_site("home")
    assert store.active_site() is None
    assert not store.remove_site("home")


def test_the_file_is_written_from_the_records(tmp_path: Path) -> None:
    """A field that no record knows, a bad path, and a duplicate site do not survive a write."""
    path = tmp_path / "outbound.json"
    path.write_text(
        json.dumps(
            {
                "active": "Lost",
                "legacy": 1,
                "sites": [
                    {"name": "Home", "paths": [["3d63"], ["xyz"], []], "colour": "red"},
                    {"name": "home", "paths": [["27d4"]]},
                    {"paths": [["27d4"]]},
                ],
                "devices": {"NOTHEX": {"name": "x"}, DEVICE: {"name": "Roof", "paths": [["3d"]]}},
            }
        ),
        encoding="utf-8",
    )
    store = OutboundStore(path)
    assert store.active_site() is None  # the active name named no site
    store.set_active("Home")
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {
        "active": "Home",
        "sites": [{"name": "Home", "paths": [["3d63"]], "kept": {}, "saved": {}}],
        "devices": {DEVICE: {"name": "Roof", "paths": [["3d"]], "kept": {}}},
    }


def test_a_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    """A file that is not JSON gives the empty answer, not an error."""
    path = tmp_path / "outbound.json"
    path.write_text("{not json", encoding="utf-8")
    assert OutboundStore(path).lookup(DEVICE) == OutboundChoice()
