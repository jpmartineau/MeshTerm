# SPDX-License-Identifier: Apache-2.0
"""The store of the sound setting of each conversation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshterm.core.sound_store import SoundStore


def test_a_conversation_with_no_record_follows_the_default(tmp_path: Path) -> None:
    """No record is ``None``: the conversation follows the default of its kind."""
    assert SoundStore(tmp_path / "sounds.json").get("chan:abc") is None


def test_a_setting_is_stored_and_read_again(tmp_path: Path) -> None:
    """A setting goes to the file, and a new store reads it back."""
    path = tmp_path / "sounds.json"
    SoundStore(path).set("dm:d4e5f6a7", "all")
    assert SoundStore(path).get("dm:d4e5f6a7") == "all"


def test_none_removes_the_record(tmp_path: Path) -> None:
    """``None`` removes the record, so the conversation follows the default again."""
    path = tmp_path / "sounds.json"
    store = SoundStore(path)
    store.set("chan:abc", "off")
    store.set("chan:abc", None)
    assert SoundStore(path).get("chan:abc") is None
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_a_bad_value_is_refused(tmp_path: Path) -> None:
    """Only ``off``, ``mentions``, and ``all`` are settings."""
    with pytest.raises(ValueError):
        SoundStore(tmp_path / "sounds.json").set("chan:abc", "loud")


def test_a_record_that_is_not_correct_is_not_read(tmp_path: Path) -> None:
    """The store keeps only the records that it knows, and the next write removes the rest."""
    path = tmp_path / "sounds.json"
    path.write_text(
        json.dumps({"chan:a": {"when": "all"}, "chan:b": {"when": "loud"}, "chan:c": 3}),
        encoding="utf-8",
    )
    store = SoundStore(path)
    assert store.get("chan:a") == "all"
    assert store.get("chan:b") is None
    store.set("chan:d", "off")
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"chan:a", "chan:d"}
