# SPDX-License-Identifier: Apache-2.0
"""The connect-time settings offer, driven end to end over a scripted device.

The scenario that motivated the position rule (JP, 2026-08-08/09): a GPS-less companion
holds its manually-set coordinates only until it restarts, then reports the 0/0 null
island — SELF_INFO always carries ``adv_lat``/``adv_lon`` as signed microdegrees / 1e6,
so "unset" arrives as exactly ``0.0``, never as a missing key (verified against the
meshcore reader). The offer must write the remembered fix straight back (filling an
empty slot overwrites nothing) and never ask the operator to weigh a position against
no position — while a device reporting a *real*, different fix still gets the honest
prompt, and non-position drift keeps prompting alongside the silent restore.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import meshterm.ui.settings_offer as offer_mod
from meshterm.core.settings_store import SettingsStore
from meshterm.ui.settings_offer import offer_remembered_settings

_PUB = "e4a392456ead6f500725758729eb9f9decccf0ccd22d1d7723131e973eeb12a4"

#: The coordinates JP's store really held when this flow was diagnosed — exact
#: microdegree quotients, so they round-trip the wire encoding byte-for-byte.
_LAT, _LON = 45.535456, -73.708698


class _ScriptedDevice:
    """A companion that answers SELF_INFO from a dict and records coordinate writes."""

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

    # The optional snapshot reads a bare bridge doesn't support — build_snapshot skips them.
    async def get_tuning(self) -> dict:
        raise RuntimeError("unsupported")

    async def get_path_hash_mode(self) -> int:
        raise RuntimeError("unsupported")

    async def get_autoadd_config(self) -> int:
        raise RuntimeError("unsupported")

    async def get_default_flood_scope(self) -> dict:
        raise RuntimeError("unsupported")


def _ctx(store: SettingsStore, device: _ScriptedDevice) -> SimpleNamespace:
    """The slice of AppContext the offer reads, over the scripted device."""
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
    """Drive the offer once, capturing whether (and with what) the prompt was shown."""
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
    """Device reports the null island, store remembers a fix → silent restore, no prompt."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    device = _ScriptedDevice(adv_lat=0.0, adv_lon=0.0)

    shown = _run(_ctx(store, device), monkeypatch)

    assert shown == []  # the operator was never bothered
    # Both coordinates were written back; the second write carries the first's value
    # (the coupled set_coords rebuilds from the updated snapshot), so the device ends
    # on the full remembered fix.
    assert device.coords_writes[-1] == (_LAT, _LON)
    # The store still remembers the fix — nothing was adopted or forgotten.
    assert store.settings(_PUB) == {"adv_lat": _LAT, "adv_lon": _LON}


def test_offer_still_asks_when_the_device_reports_a_real_conflicting_fix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A device with an actual different position is a true conflict: prompt, don't write."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    device = _ScriptedDevice(adv_lat=45.533451, adv_lon=-73.710235)

    shown = _run(_ctx(store, device), monkeypatch, choice=None)  # operator dismisses

    assert len(shown) == 1 and {d.key for d in shown[0]} == {"adv_lat", "adv_lon"}
    assert device.coords_writes == []  # dismissing wrote nothing
    assert store.settings(_PUB) == {"adv_lat": _LAT, "adv_lon": _LON}


def test_offer_splits_a_mixed_drift_between_silent_position_and_prompted_rest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Position restores silently while the other drifted settings still reach the prompt."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    store.remember(_PUB, "name", "Homestead-Pico")
    device = _ScriptedDevice(adv_lat=0.0, adv_lon=0.0, name="MeshCore-abcd")

    shown = _run(_ctx(store, device), monkeypatch, choice=None)

    assert device.coords_writes[-1] == (_LAT, _LON)  # the fix came back silently
    assert len(shown) == 1 and [d.key for d in shown[0]] == ["name"]  # only the real conflict


def test_offer_stays_quiet_when_nothing_is_remembered(tmp_path: Path, monkeypatch) -> None:
    """A device with no remembered settings pays one probe and sees nothing — no writes."""
    store = SettingsStore(tmp_path / "settings.json")
    device = _ScriptedDevice(adv_lat=0.0, adv_lon=0.0)

    shown = _run(_ctx(store, device), monkeypatch)

    assert shown == [] and device.coords_writes == []


class _GpsDevice(_ScriptedDevice):
    """A companion with a GPS, reporting its ``gps`` switch as the firmware does."""

    def __init__(self, *, gps: str, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self._gps = gps

    async def get_custom_vars(self) -> dict[str, str]:
        return {"gps": self._gps, "gps_interval": "0"}


def test_offer_leaves_a_position_the_gps_moved_alone(tmp_path: Path, monkeypatch) -> None:
    """With its GPS running, a node somewhere else is a reading: no prompt and no write."""
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
    """A GPS that isn't running moved nothing, so a different position is a real conflict."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    device = _GpsDevice(gps="0", adv_lat=46.813878, adv_lon=-71.207981)

    shown = _run(_ctx(store, device), monkeypatch)

    assert len(shown) == 1 and {d.key for d in shown[0]} == {"adv_lat", "adv_lon"}


def test_a_gps_with_no_fix_yet_still_gets_the_saved_position(tmp_path: Path, monkeypatch) -> None:
    """Before its first fix the node reports none, and the saved one holds until it comes."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(_PUB, "adv_lat", _LAT)
    store.remember(_PUB, "adv_lon", _LON)
    device = _GpsDevice(gps="1", adv_lat=0.0, adv_lon=0.0)

    shown = _run(_ctx(store, device), monkeypatch)

    assert shown == [] and device.coords_writes[-1] == (_LAT, _LON)
