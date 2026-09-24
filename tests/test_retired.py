# SPDX-License-Identifier: Apache-2.0
"""A retired name is never reused.

A value can outlive its key: a file nobody has saved since the key left still holds it. If
the name came back with a new meaning — seconds become minutes, a range moves — an old
value that happens to pass the new spec would load cleanly and mean something else, and
no validation can catch that. So every name that leaves is recorded as retired, and these
tests fail the moment one comes back:

* the preference registry against :data:`meshterm.core.preferences.RETIRED`, and the
  device-setting registry against :data:`meshterm.core.device_config.RETIRED`;
* every persisted record type — a dataclass in :mod:`meshterm.core` declaring its own
  ``RETIRED`` set — against its own fields. The record is the path: a name retired from
  one record says nothing about another, where it may be exactly the right name.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import logging
import pkgutil
from pathlib import Path

import pytest

import meshterm.core
from meshterm.core import device_config, preferences
from meshterm.core.admin_store import AdminStore
from meshterm.core.models import Contact
from meshterm.core.preferences import (
    PREFERENCES,
    RETIRED,
    PreferenceError,
    Preferences,
    report_dropped,
)
from meshterm.core.remote_store import RemoteStore
from meshterm.core.settings_store import SettingsStore


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


def test_no_live_device_setting_takes_a_retired_key() -> None:
    """The settings store restores values onto the radio; a reused key would misplace one."""
    assert not {spec.key for spec in device_config.DEVICE_SETTINGS} & device_config.RETIRED


#: Every persisted record type, by the module that writes it. A store that grows a record
#: adds it here, and the sweep below checks it without being told how.
_EXPECTED_RECORDS = {
    "admin_store": {"AdminCredential"},
    "advert_store": {"_Device", "_Document"},
    "channel_store": {"RememberedChannel"},
    "contact_store": {"RememberedContact"},
    "courier_store": {"QueuedMessage"},
    "device_store": {"RememberedDevice"},
    "remote_store": {"CachedValue", "_RemoteNode"},
    "watch_store": {"WatchedNode", "Alert", "_State"},
}


def test_the_record_walk_finds_every_store() -> None:
    """The sweep below is not vacuous: it sees each store's records."""
    found = {(cls.__module__.rsplit(".", 1)[1], cls.__qualname__) for cls in _record_types()}
    expected = {(module, name) for module, names in _EXPECTED_RECORDS.items() for name in names}
    assert expected <= found


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


# -- the stores write only what they know ----------------------------------------------

_NODE = Contact(name="Hilltop-Repeater", public_key="cd" * 32, key_prefix="cd" * 6)


def test_the_admin_store_writes_only_known_fields(tmp_path: Path) -> None:
    """A stray field on a credential, or a stray record, is gone after the next write."""
    path = tmp_path / "admin.json"
    path.write_text(
        json.dumps(
            {
                "cd" * 32: {"password": "pw", "label": "H", "last_used": "", "role": "x"},
                "junk": {"no": "password"},
            }
        )
    )
    store = AdminStore(path)
    assert store.get(_NODE) == "pw"
    store.remember(Contact(name="Other", public_key="ef" * 32, key_prefix="ef" * 6), "pw2")
    written = json.loads(path.read_text())
    assert set(written) == {"cd" * 32, "ef" * 32}
    assert written["cd" * 32].keys() == {"password", "label", "last_used"}


def test_the_remote_store_writes_only_known_fields(tmp_path: Path) -> None:
    """Unknown fields on a node or a cached value do not survive, and the spelling holds."""
    path = tmp_path / "remote.json"
    stamp = "2026-09-01T12:00:00+00:00"
    path.write_text(
        json.dumps(
            {
                "cd" * 32: {
                    "settings": {
                        "txdelay": {"value": "0.5", "read_at": stamp, "unit": "s"},
                        "bridge.delay": {"unsupported": True, "read_at": stamp},
                    },
                    "history": ["ver"],
                    "notes": "stray",
                }
            }
        )
    )
    store = RemoteStore(path)
    store.append_history(_NODE, "clock")
    record = json.loads(path.read_text())["cd" * 32]
    assert record.keys() == {"settings", "history"}
    assert record["settings"]["txdelay"] == {"value": "0.5", "read_at": stamp}
    assert record["settings"]["bridge.delay"] == {"unsupported": True, "read_at": stamp}
    assert record["history"] == ["ver", "clock"]


def test_the_settings_store_keeps_only_registered_settings(tmp_path: Path) -> None:
    """A key that is not a device setting is never offered for restore, and leaves the file."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"devices": {"ab" * 32: {"name": "Node", "no_such_key": 3}}}))
    store = SettingsStore(path)
    assert store.settings("ab" * 32) == {"name": "Node"}
    store.remember("ab" * 32, "tx_power", 14)
    assert json.loads(path.read_text())["devices"]["ab" * 32] == {"name": "Node", "tx_power": 14}
