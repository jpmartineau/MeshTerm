# SPDX-License-Identifier: Apache-2.0
"""The settings offer at connect time, run from start to end over a scripted device.

This scenario is the reason for the position rule (JP, 2026-08-08/09). A companion with no
GPS keeps its coordinates, which the user set by hand, only until it restarts. Then it
reports the 0/0 null island. SELF_INFO always has ``adv_lat`` and ``adv_lon`` as signed
microdegrees / 1e6, so "unset" arrives as exactly ``0.0``, never as a missing key (we
checked this against the meshcore reader).

The offer must write the remembered fix back at once. A write into an empty slot overwrites
nothing. The offer must never ask the user to weigh a position against no position. But a
device that reports a real, different fix still gets the honest prompt. A drift of a
setting that is not a position still prompts, together with the silent restore.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import meshterm.ui.settings_offer as offer_mod
from meshterm.core.settings_store import SettingsStore
from meshterm.ui.settings_offer import offer_remembered_settings

_PUB = "e4a392456ead6f500725758729eb9f9decccf0ccd22d1d7723131e973eeb12a4"

#: The coordinates that the store of JP held when we diagnosed this flow. They are exact
#: microdegree quotients, so they go through the wire encoding and come back byte for byte.
_LAT, _LON = 45.535456, -73.708698


class _ScriptedDevice:
    """A companion that answers SELF_INFO from a dict and records the writes of coordinates."""

    def __init__(self, *, adv_lat: float, adv_lon: float, name: str = "Homestead-Pico") -> None:
        self._info = {
            "public_key": _PUB,
            "name": name,
            "adv_lat": adv_lat,
            "adv_lon": adv_lon,
        }
        self.coords_writes: list[tuple[float, float]] = []

    async def get_self_info(self) -> dict:
        return dict(self._info)

    async def set_coords(self, lat: float, lon: float) -> None:
        self.coords_writes.append((lat, lon))
        self._info["adv_lat"], self._info["adv_lon"] = lat, lon

    async def set_name(self, name: str) -> None:
        self._info["name"] = name

    # A bare bridge does not support these optional reads for the snapshot. build_snapshot
    # skips them.
    async def get_tuning(self) -> dict:
        raise RuntimeError("unsupported")

    async def get_path_hash_mode(self) -> int:
        raise RuntimeError("unsupported")

    async def get_autoadd_config(self) -> int:
        raise RuntimeError("unsupported")

    async def get_default_flood_scope(self) -> dict:
        raise RuntimeError("unsupported")


def _ctx(store: SettingsStore, device: _ScriptedDevice) -> SimpleNamespace:
    """The part of AppContext that the offer reads, with the scripted device."""
    import logging

    async def dev() -> _ScriptedDevice:
        return device

    return SimpleNamespace(
        settings_store=store,
        is_connected=True,
        device=dev,
        devstate=SimpleNamespace(invalidate_config=lambda: None),
        log=logging.getLogger("test-settings-offer"),
    )


def _run(ctx, monkeypatch, *, choice=None):  # noqa: ANN001, ANN202
    """Run the offer one time. Record whether the prompt showed, and with what content."""
    shown: list[list] = []

    async def fake_prompt(_ctx, drifted):  # noqa: ANN001, ANN202
        shown.append(list(drifted))
        return choice

    monkeypatch.setattr(offer_mod, "_prompt", fake_prompt)
    asyncio.run(offer_remembered_settings(ctx))
    return shown


def test_offer_restores_a_position_the_device_lost_without_asking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The device reports the null island and the store has a fix: the offer restores it.

    The offer does not show a prompt.
    """
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    device = _ScriptedDevice(adv_lat=0.0, adv_lon=0.0)

    shown = _run(_ctx(store, device), monkeypatch)

    assert shown == []  # the user was never asked
    # The offer wrote both coordinates back. The second write has the value of the first
    # (the coupled set_coords builds again from the changed snapshot), so the device ends
    # with the full remembered fix.
    assert device.coords_writes[-1] == (_LAT, _LON)
    # The store still has the fix. The offer adopted nothing and forgot nothing.
    assert store.settings(_PUB) == {"adv_lat": _LAT, "adv_lon": _LON}


def test_offer_still_asks_when_the_device_reports_a_real_conflicting_fix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A device with a real, different position is a true conflict.

    The offer prompts and does not write.
    """
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    device = _ScriptedDevice(adv_lat=45.533451, adv_lon=-73.710235)

    shown = _run(_ctx(store, device), monkeypatch, choice=None)  # the user dismisses the prompt

    assert len(shown) == 1 and {d.key for d in shown[0]} == {"adv_lat", "adv_lon"}
    assert device.coords_writes == []  # the dismissal wrote nothing
    assert store.settings(_PUB) == {"adv_lat": _LAT, "adv_lon": _LON}


def test_offer_splits_a_mixed_drift_between_silent_position_and_prompted_rest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The offer restores the position silently.

    The other drifted settings still reach the prompt.
    """
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    store.remember(_PUB, "name", "Homestead-Pico")
    device = _ScriptedDevice(adv_lat=0.0, adv_lon=0.0, name="MeshCore-abcd")

    shown = _run(_ctx(store, device), monkeypatch, choice=None)

    assert device.coords_writes[-1] == (_LAT, _LON)  # the fix returned silently
    assert len(shown) == 1 and [d.key for d in shown[0]] == ["name"]  # only the real conflict


def test_offer_stays_quiet_when_nothing_is_remembered(tmp_path: Path, monkeypatch) -> None:
    """For a device with no remembered settings, the offer does one probe and finds nothing.

    The offer writes nothing.
    """
    store = SettingsStore(tmp_path / "settings.json")
    device = _ScriptedDevice(adv_lat=0.0, adv_lon=0.0)

    shown = _run(_ctx(store, device), monkeypatch)

    assert shown == [] and device.coords_writes == []


class _GpsDevice(_ScriptedDevice):
    """A companion with a GPS. It reports its ``gps`` switch as the firmware does."""

    def __init__(self, *, gps: str, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self._gps = gps

    async def get_custom_vars(self) -> dict[str, str]:
        return {"gps": self._gps, "gps_interval": "0"}


def test_offer_leaves_a_position_the_gps_moved_alone(tmp_path: Path, monkeypatch) -> None:
    """If the GPS runs, a node in a new place is a reading. The offer does not prompt or write."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    device = _GpsDevice(gps="1", adv_lat=46.813878, adv_lon=-71.207981)

    shown = _run(_ctx(store, device), monkeypatch)

    assert shown == [] and device.coords_writes == []
    assert store.settings(_PUB) == {"adv_lat": _LAT, "adv_lon": _LON}


def test_offer_still_asks_about_a_moved_position_with_the_gps_off(
    tmp_path: Path, monkeypatch
) -> None:
    """A GPS that does not run moved nothing, so a different position is a real conflict."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    device = _GpsDevice(gps="0", adv_lat=46.813878, adv_lon=-71.207981)

    shown = _run(_ctx(store, device), monkeypatch)

    assert len(shown) == 1 and {d.key for d in shown[0]} == {"adv_lat", "adv_lon"}


def test_a_gps_with_no_fix_yet_still_gets_the_saved_position(tmp_path: Path, monkeypatch) -> None:
    """Before its first fix, the node reports none. The saved fix stays until a new fix comes."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    device = _GpsDevice(gps="1", adv_lat=0.0, adv_lon=0.0)

    shown = _run(_ctx(store, device), monkeypatch)

    assert shown == [] and device.coords_writes[-1] == (_LAT, _LON)
