# SPDX-License-Identifier: Apache-2.0
"""Tests for the memory of device settings across sessions (``core.settings_store``).

The tests cover three parts. The first is the durable store for each device. The second is
the drift detection, which compares the remembered values with the live snapshot of a device.
The third is the ``restore`` and ``adopt`` reconcile actions that the offer at startup runs.
The reconcile tests run against the :class:`MockDevice` simulator. Its configuration resets
to the firmware defaults at each construction. This is like the radio bridge with no firmware,
which forgets its settings at a restart.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.connection import make_device
from meshterm.core.device_config import build_snapshot
from meshterm.core.device_store import DeviceStore
from meshterm.core.settings_store import (
    SettingsStore,
    adopt,
    restore,
    settings_drift,
)
from meshterm.persistence.repository import Repository
from meshterm.tools.config import apply_ops

PUB_A = "aa" * 32
PUB_B = "bb" * 32


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """An application context that uses the mock, with the plain (console) UI surface."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "cfg.db")
    context = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


# --- the store ---------------------------------------------------------------


def test_store_round_trips_and_persists(tmp_path: Path) -> None:
    """Remembered settings stay in a new store instance.

    The store keeps them for each device.
    """
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.remember(PUB_A, "name", "Ops-Node")
    store.remember(PUB_A, "radio_freq", 915.0)
    store.remember(PUB_B, "tx_power", 20)

    reloaded = SettingsStore(path)  # a new process reads the same file
    assert reloaded.settings(PUB_A) == {"name": "Ops-Node", "radio_freq": 915.0}
    assert reloaded.settings(PUB_B) == {"tx_power": 20}  # a second device has its own set


def test_store_replace_forget_and_forget_all(tmp_path: Path) -> None:
    """A new remember of a key replaces it. forget removes one key. forget_all clears the device."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(PUB_A, "name", "First")
    store.remember(PUB_A, "name", "Second")  # the same key, a new value
    store.remember(PUB_A, "tx_power", 20)
    assert store.settings(PUB_A) == {"name": "Second", "tx_power": 20}

    store.forget(PUB_A, "name")
    assert store.settings(PUB_A) == {"tx_power": 20}

    store.forget_all(PUB_A)
    assert store.settings(PUB_A) == {}
    # When the device has nothing remembered, the stored file does not have the device.
    assert json.loads((tmp_path / "settings.json").read_text())["devices"] == {}


def test_store_normalises_device_key(tmp_path: Path) -> None:
    """A device key matches in any case, and the store removes an optional 0x prefix."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember("AABB", "name", "Ops")
    assert store.settings("0xaabb") == {"name": "Ops"}


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    """A file with invalid data reads as empty instead of raising an error, and stays writable."""
    path = tmp_path / "settings.json"
    path.write_text("not json at all", encoding="utf-8")
    store = SettingsStore(path)
    assert store.settings(PUB_A) == {}
    store.remember(PUB_A, "name", "Ops")  # the store recovers and stores the value
    assert SettingsStore(path).settings(PUB_A) == {"name": "Ops"}


def test_store_drops_malformed_values(tmp_path: Path) -> None:
    """The store skips non-scalar values (a container, a null). It keeps scalar values."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "devices": {
                    PUB_A: {"name": "Good", "bad_list": [1, 2], "bad_null": None, "tx_power": 20}
                }
            }
        ),
        encoding="utf-8",
    )
    assert SettingsStore(path).settings(PUB_A) == {"name": "Good", "tx_power": 20}


def test_store_ignores_non_scalar_remember(tmp_path: Path) -> None:
    """A remember of a non-scalar value does nothing.

    The store keeps only strings, numbers, and bools.
    """
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(PUB_A, "name", ["not", "a", "scalar"])
    assert store.settings(PUB_A) == {}


# --- drift -------------------------------------------------------------------


async def _mock_device():
    device = make_device(mock=True, port=None)
    await device.connect()
    pubkey = (await device.get_self_info())["public_key"]
    return device, pubkey


async def test_drift_reports_only_changed_settings(tmp_path: Path) -> None:
    """Drift lists the remembered settings that are different from the device, in registry order."""
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(pubkey, "radio_freq", 915.0)  # the device default is 869.618: drift
    store.remember(pubkey, "name", "MockCompanion")  # the same as the device default: no drift

    snapshot = await build_snapshot(device)
    drifted = settings_drift(store, pubkey, snapshot)
    assert [d.key for d in drifted] == ["radio_freq"]
    assert drifted[0].remembered == 915.0 and drifted[0].current == 869.618


async def test_drift_empty_when_nothing_remembered(tmp_path: Path) -> None:
    """A device with no remembered settings never shows drift. Firmware radios have no cost."""
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    snapshot = await build_snapshot(device)
    assert settings_drift(store, pubkey, snapshot) == []


async def test_drift_skips_unknown_keys(tmp_path: Path) -> None:
    """The drift ignores a remembered key that the registry does not define.

    It does not report the key.
    """
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(pubkey, "gone_from_registry", "whatever")
    snapshot = await build_snapshot(device)
    assert settings_drift(store, pubkey, snapshot) == []


# --- restore / adopt ---------------------------------------------------------


async def test_restore_writes_remembered_values_including_coupled(tmp_path: Path) -> None:
    """Restore writes the saved values to the device.

    It builds the coupled radio fields together.
    """
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(pubkey, "name", "Ops-Node")
    store.remember(pubkey, "radio_freq", 915.0)
    store.remember(pubkey, "radio_sf", 12)  # a second radio field: this tests the coupled apply

    snapshot = await build_snapshot(device)
    drifted = settings_drift(store, pubkey, snapshot)
    written = await restore(store, device, snapshot, [d.key for d in drifted])
    assert written == 3

    after = await build_snapshot(device)
    assert after["name"] == "Ops-Node"
    assert after["radio_freq"] == 915.0
    assert after["radio_sf"] == 12
    assert after["radio_bw"] == 62.5  # the coupled rebuild keeps a radio field that was not changed
    # Idempotent: the device now matches, so nothing is left to restore.
    assert settings_drift(store, pubkey, after) == []


async def test_adopt_updates_store_to_device_values(tmp_path: Path) -> None:
    """Adopt takes the current values of the device as the new saved values.

    This clears the drift.
    """
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(pubkey, "radio_freq", 915.0)  # different from the 869.618 of the device

    snapshot = await build_snapshot(device)
    drifted = settings_drift(store, pubkey, snapshot)
    adopt(store, pubkey, snapshot, [d.key for d in drifted])

    assert store.settings(pubkey)["radio_freq"] == 869.618  # now the same as the device
    assert settings_drift(store, pubkey, snapshot) == []


async def test_adopt_forgets_a_setting_the_device_no_longer_reports(tmp_path: Path) -> None:
    """Adopt forgets a key that the device reports with no value. It does not store a null."""
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(pubkey, "flood_scope", "#ops")

    snapshot = await build_snapshot(device)
    snapshot.pop("flood_scope", None)  # simulate firmware that does not report this field
    adopt(store, pubkey, snapshot, ["flood_scope"])
    assert "flood_scope" not in store.settings(pubkey)


# --- write-through -----------------------------------------------------------


async def test_apply_setting_remembers_through_the_config_executor(ctx) -> None:
    """The store records each setting that the config executor changes, under the device key."""
    device = await ctx.device()
    pubkey = (await device.get_self_info())["public_key"]
    snapshot = await build_snapshot(device)

    changes, _artifacts, _report = await apply_ops(
        ctx, device, snapshot, [("set", "name", "Ops-Node"), ("set", "tx_power", "18")]
    )
    assert changes == 2
    assert ctx.settings_store.settings(pubkey) == {"name": "Ops-Node", "tx_power": 18}
