# SPDX-License-Identifier: Apache-2.0
"""A retired name is never reused.

A value can outlive its key: a file nobody has saved since the key left still holds it. If
the name came back with a new meaning — seconds become minutes, a range moves — an old
value that happens to pass the new spec would load cleanly and mean something else, and
no validation can catch that. So every name that leaves is recorded as retired, and these
tests fail the moment one comes back:

* the preference registry against :data:`meshterm.core.preferences.RETIRED`;
* every persisted record type — a dataclass in :mod:`meshterm.core` declaring its own
  ``RETIRED`` set — against its own fields. The record is the path: a name retired from
  one record says nothing about another, where it may be exactly the right name.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import pkgutil
from pathlib import Path

import pytest

import meshterm.core
from meshterm.core import preferences
from meshterm.core.preferences import (
    PREFERENCES,
    RETIRED,
    PreferenceError,
    Preferences,
    report_dropped,
)


def _record_types() -> list[type]:
    """Every dataclass in meshterm.core that declares a ``RETIRED`` set of field names."""
    found = []
    for info in pkgutil.iter_modules(meshterm.core.__path__):
        module = importlib.import_module(f"meshterm.core.{info.name}")
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and value.__module__ == module.__name__
                and dataclasses.is_dataclass(value)
                and isinstance(getattr(value, "RETIRED", None), frozenset)
            ):
                found.append(value)
    return found


def test_no_live_preference_takes_a_retired_key() -> None:
    """A preference that returns changed returns under a new name."""
    assert not {spec.key for spec in PREFERENCES} & RETIRED


def test_the_record_walk_finds_the_stores() -> None:
    """The sweep below is not vacuous: it sees at least the advert store's records."""
    names = {cls.__qualname__ for cls in _record_types()}
    assert {"_Device", "_Document"} <= names


@pytest.mark.parametrize("record", _record_types(), ids=lambda cls: cls.__qualname__)
def test_no_record_takes_back_a_retired_field(record: type) -> None:
    """Each persisted record's fields are disjoint from the names it has retired."""
    fields = {f.name for f in dataclasses.fields(record)}
    assert not fields & record.RETIRED


def test_loading_refuses_a_retired_key(tmp_path: Path) -> None:
    """A retired key in the file is not applied, and is named as retired."""
    path = tmp_path / "preferences.toml"
    retired = sorted(RETIRED)[0]
    path.write_text(f"{retired} = 3\ntrace_cooldown_s = 2.0\n", encoding="utf-8")
    prefs = Preferences.load(path)
    assert prefs.dropped == {retired: preferences.DROPPED_RETIRED}
    assert prefs.trace_cooldown_s == 2.0


def test_the_next_save_leaves_out_everything_refused(tmp_path: Path) -> None:
    """Retired, unknown and invalid entries are all gone once the file is saved."""
    path = tmp_path / "preferences.toml"
    retired = sorted(RETIRED)[0]
    path.write_text(
        f"{retired} = 3\ntrace_cooldwn_s = 2.0\nhistory_days = -4\ntrace_cooldown_s = 2.0\n",
        encoding="utf-8",
    )
    prefs = Preferences.load(path)
    assert set(prefs.dropped) == {retired, "trace_cooldwn_s", "history_days"}
    prefs.save()
    text = path.read_text(encoding="utf-8")
    for key in (retired, "trace_cooldwn_s", "history_days"):
        assert f"{key} =" not in text
    assert "trace_cooldown_s = 2.0" in text


def test_a_retired_key_is_named_as_such_when_set(tmp_path: Path) -> None:
    """`preferences set` on a retired key says it is no longer a preference."""
    with pytest.raises(PreferenceError, match="no longer a preference"):
        Preferences().set(sorted(RETIRED)[0], 1)


def test_drops_are_logged_by_kind(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A retired key is expected (INFO); a typo or a bad value is a WARNING naming it."""
    path = tmp_path / "preferences.toml"
    retired = sorted(RETIRED)[0]
    path.write_text(f"{retired} = 3\ntrace_cooldwn_s = 2.0\nhistory_days = -4\n", encoding="utf-8")
    log = logging.getLogger("test_retired")
    with caplog.at_level(logging.INFO, logger="test_retired"):
        report_dropped(Preferences.load(path), log)
    by_level = {(r.levelno, r.getMessage()) for r in caplog.records}
    assert any(level == logging.INFO and retired in msg for level, msg in by_level)
    assert any(level == logging.WARNING and "trace_cooldwn_s" in msg for level, msg in by_level)
    assert any(level == logging.WARNING and "history_days" in msg for level, msg in by_level)
