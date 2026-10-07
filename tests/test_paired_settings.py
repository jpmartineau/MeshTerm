# SPDX-License-Identifier: Apache-2.0
"""Tests for settings that share one device command, and for the read back of the PIN.

The two problems were found when a companion was restored from its own backup after a
reflash.

The latitude and longitude go to the firmware together, and so do the four radio
parameters. Thus a change to one setting means that MeshTerm sends its siblings again,
unchanged. The siblings come from the snapshot of the caller. The caller reads the
snapshot one time, before the first write. A restore that changed both coordinates did
this: it applied ``adv_lat`` (and kept the old longitude), then it applied ``adv_lon``
(and kept the old latitude). The second write undid the first, and no message showed it.
On the real device, the latitude stayed at 0.0 and the frequency stayed at the factory
default, and each op reported success.
"""

from __future__ import annotations

import asyncio

import pytest

from meshterm.core.device_config import (
    DEVICE_SETTINGS,
    DeviceConfigError,
    build_snapshot,
    get_spec,
    parse_value,
)


class _RecordingDevice:
    """Stores the coupled commands, and models the firmware: the last write wins."""

    def __init__(self, **state) -> None:
        self.state = dict(state)
        self.radio_calls: list[tuple] = []
        self.coord_calls: list[tuple] = []
        self.tuning_calls: list[tuple] = []
        self.repeat_calls: list = []

    async def set_radio(self, freq, bw, sf, cr, repeat=None) -> None:
        self.radio_calls.append((freq, bw, sf, cr))
        self.repeat_calls.append(repeat)
        self.state.update(radio_freq=freq, radio_bw=bw, radio_sf=sf, radio_cr=cr)
        if repeat is not None:
            self.state["repeat"] = repeat

    async def set_coords(self, lat, lon) -> None:
        self.coord_calls.append((lat, lon))
        self.state.update(adv_lat=lat, adv_lon=lon)

    async def set_tuning(self, rx_delay, airtime_factor) -> None:
        self.tuning_calls.append((rx_delay, airtime_factor))
        self.state.update(rx_delay=rx_delay, airtime_factor=airtime_factor)


async def _apply_all(device, snapshot, changes):
    """Apply each change in the way that a restore or a batch of staged edits does: in sequence."""
    for key, raw in changes:
        spec = get_spec(key)
        await spec.apply(device, parse_value(spec, raw, snapshot), snapshot)


def test_both_coordinates_survive_being_applied_together() -> None:
    """Both coordinates survive when they are applied together.

    This is the bug: the second coordinate must not send the old value of the first
    coordinate again. A restore of a backup onto a node with factory settings changes both
    at one time. Before the correction, the device ended at ``(0.0, -73.708724)``. The
    longitude was correct, and the latitude went back to the old value with no message.
    """
    device = _RecordingDevice(adv_lat=0.0, adv_lon=0.0)
    snapshot = dict(device.state)

    asyncio.run(
        _apply_all(
            device,
            snapshot,
            [
                ("adv_lat", "45.535445"),
                ("adv_lon", "-73.708724"),
            ],
        )
    )

    assert device.state["adv_lat"] == pytest.approx(45.535445)
    assert device.state["adv_lon"] == pytest.approx(-73.708724)
    # The last write must have both values. It must not have one real value and one old zero.
    assert device.coord_calls[-1] == pytest.approx((45.535445, -73.708724))


def test_the_order_the_coordinates_are_applied_in_does_not_matter() -> None:
    """The order of the coordinates does not matter.

    Neither order can lose a value. The code must not depend on the order of the ops in a
    plan.
    """
    for changes in (
        [("adv_lat", "45.5"), ("adv_lon", "-73.7")],
        [("adv_lon", "-73.7"), ("adv_lat", "45.5")],
    ):
        device = _RecordingDevice(adv_lat=0.0, adv_lon=0.0)
        asyncio.run(_apply_all(device, dict(device.state), changes))
        assert (device.state["adv_lat"], device.state["adv_lon"]) == pytest.approx((45.5, -73.7)), (
            changes
        )


def test_two_radio_fields_applied_together_both_stick() -> None:
    """Two radio fields that are applied together both stay.

    The frequency went back to the factory default when the spreading factor came after
    it.
    """
    device = _RecordingDevice(radio_freq=869.618, radio_bw=62.5, radio_sf=8, radio_cr=5)
    snapshot = dict(device.state)

    asyncio.run(
        _apply_all(
            device,
            snapshot,
            [
                ("radio_freq", "910.525"),
                ("radio_sf", "7"),
            ],
        )
    )

    assert device.state["radio_freq"] == pytest.approx(910.525)
    assert device.state["radio_sf"] == 7
    assert device.state["radio_bw"] == pytest.approx(62.5)  # the siblings that did not change stay
    assert device.state["radio_cr"] == 5


def test_all_four_radio_fields_at_once() -> None:
    """A change of all four radio fields is the worst case.

    A sibling can become old in three places.
    """
    device = _RecordingDevice(radio_freq=869.618, radio_bw=250.0, radio_sf=8, radio_cr=8)

    asyncio.run(
        _apply_all(
            device,
            dict(device.state),
            [
                ("radio_freq", "910.525"),
                ("radio_bw", "62.5"),
                ("radio_sf", "7"),
                ("radio_cr", "5"),
            ],
        )
    )

    assert device.radio_calls[-1] == pytest.approx((910.525, 62.5, 7, 5))


def test_both_tuning_fields_applied_together_both_stick() -> None:
    """``rx_delay`` and ``airtime_factor`` share a command, with the same rules."""
    device = _RecordingDevice(rx_delay=0.0, airtime_factor=0.0)

    asyncio.run(
        _apply_all(
            device,
            dict(device.state),
            [
                ("rx_delay", "1.5"),
                ("airtime_factor", "2.0"),
            ],
        )
    )

    assert device.tuning_calls[-1] == pytest.approx((1.5, 2.0))


def test_a_coupled_apply_leaves_the_snapshot_describing_the_device() -> None:
    """A coupled apply leaves the snapshot as a description of the device.

    The write-back is the mechanism, so the test states it: the snapshot follows what the
    code set.
    """
    device = _RecordingDevice(adv_lat=0.0, adv_lon=0.0)
    snapshot = dict(device.state)

    asyncio.run(_apply_all(device, snapshot, [("adv_lat", "45.5")]))

    assert snapshot["adv_lat"] == pytest.approx(45.5)


# --- the read back of the Device PIN ----------------------------------------------------


class _InfoDevice:
    """A device whose two info frames have different keys, as the frames of real firmware do."""

    def __init__(self, self_info: dict, device_info: dict) -> None:
        self._self_info = self_info
        self._device_info = device_info

    async def get_self_info(self) -> dict:
        return dict(self._self_info)

    async def get_device_info(self) -> dict:
        return dict(self._device_info)

    async def get_tuning(self):
        raise NotImplementedError

    async def get_path_hash_mode(self):
        raise NotImplementedError

    async def get_autoadd_config(self):
        raise NotImplementedError

    async def get_default_flood_scope(self):
        raise NotImplementedError


def test_the_snapshot_folds_in_the_device_query_frame() -> None:
    """The snapshot includes the device-query frame.

    ``ble_pin`` is only in the device-query payload, so the snapshot must read it.
    """
    device = _InfoDevice({"name": "Homestead"}, {"ble_pin": 701307, "model": "T1000-E"})

    snapshot = asyncio.run(build_snapshot(device))

    assert snapshot["ble_pin"] == 701307
    assert snapshot["model"] == "T1000-E"
    assert snapshot["name"] == "Homestead"


def test_self_info_wins_where_the_two_frames_overlap() -> None:
    """SELF_INFO wins where the two frames overlap.

    SELF_INFO is the authority for each value that it reports. The device query only fills
    the gaps.
    """
    device = _InfoDevice({"name": "from-self-info"}, {"name": "from-device-query"})

    assert asyncio.run(build_snapshot(device))["name"] == "from-self-info"


def test_the_device_pin_row_reads_the_firmware_s_own_name() -> None:
    """The Device PIN row reads the own name of the firmware.

    The row showed "?" on each real companion. The code wrote the key as ``device_pin``,
    but the firmware reports it as ``ble_pin``.
    """
    spec = get_spec("device_pin")

    assert spec.getter({"ble_pin": 701307}) == 701307
    assert spec.getter({"device_pin": 424242}) == 424242  # the getter accepts the canonical key
    assert spec.getter({}) is None  # an unknown value stays unknown


def test_a_snapshot_read_failure_still_yields_the_rest() -> None:
    """A failure to read the snapshot still gives the other values.

    The device-query frame is optional. Older firmware adds nothing from it.
    """

    class _NoQuery(_InfoDevice):
        async def get_device_info(self):
            raise RuntimeError("firmware predates the device query")

    snapshot = asyncio.run(build_snapshot(_NoQuery({"name": "Homestead"}, {})))
    assert snapshot["name"] == "Homestead"
    assert get_spec("device_pin").getter(snapshot) is None


def test_every_coupled_setting_is_covered_here() -> None:
    """These tests cover each coupled setting.

    A new setting that shares a coupled command must be added to these tests.
    """
    coupled = {
        "adv_lat",
        "adv_lon",
        "radio_freq",
        "radio_bw",
        "radio_sf",
        "radio_cr",
        "rx_delay",
        "airtime_factor",
        "client_repeat",
        "autoadd_max_hops",
    }
    known = {spec.key for spec in DEVICE_SETTINGS}
    assert coupled <= known, coupled - known


# -- client repeat: the byte at the end of the radio command ------------------------------


def test_retuning_the_radio_restates_client_repeat() -> None:
    """A retune of the radio states client repeat again.

    This is the trap: the firmware reads a radio command with no repeat byte as "stop
    relaying". Thus a companion that relayed for its neighbours stopped when anyone changed
    its spreading factor, and no message said this.
    """
    device = _RecordingDevice(
        radio_freq=869.495, radio_bw=62.5, radio_sf=8, radio_cr=5, repeat=True
    )
    snapshot = dict(device.state)
    asyncio.run(_apply_all(device, snapshot, [("radio_sf", "9")]))
    assert device.repeat_calls == [True]


def test_firmware_without_client_repeat_is_sent_no_repeat_byte() -> None:
    """MeshTerm sends no repeat byte to firmware that has no client repeat.

    Firmware that never reported the setting is older than the byte, so MeshTerm sends no
    byte.
    """
    device = _RecordingDevice(radio_freq=869.525, radio_bw=250.0, radio_sf=11, radio_cr=5)
    snapshot = dict(device.state)
    asyncio.run(_apply_all(device, snapshot, [("radio_sf", "10")]))
    assert device.repeat_calls == [None]


def test_the_simulator_models_the_trap_the_restating_avoids() -> None:
    """The simulator models the trap that the repeated statement avoids.

    A radio command with no repeat byte turns the relaying of the simulator off. The
    command of the registry keeps it on.
    """
    from meshterm.core.connection import MockDevice

    async def scenario() -> tuple[bool, bool]:
        device = MockDevice()
        await device.connect()
        await device.set_radio(869.495, 62.5, 8, 5, repeat=True)
        await device.set_radio(869.495, 62.5, 9, 5)  # without the byte
        dropped = (await device.get_device_info())["repeat"]
        await device.set_radio(869.495, 62.5, 8, 5, repeat=True)
        snapshot = await build_snapshot(device)
        spec = get_spec("radio_sf")
        await spec.apply(device, parse_value(spec, "9", snapshot), snapshot)
        return dropped, (await device.get_device_info())["repeat"]

    dropped, kept = asyncio.run(scenario())
    assert dropped is False
    assert kept is True


def test_client_repeat_restates_the_radio_it_rides_with() -> None:
    """Client repeat states again the radio that it travels with.

    A switch of the relaying sends the four radio fields again, unchanged, next to it.
    """
    device = _RecordingDevice(
        radio_freq=869.495,
        radio_bw=62.5,
        radio_sf=8,
        radio_cr=5,
        repeat=False,
        repeat_freqs=[(869.495, 869.495)],
    )
    snapshot = dict(device.state)
    asyncio.run(_apply_all(device, snapshot, [("client_repeat", "on")]))
    assert device.radio_calls == [(869.495, 62.5, 8, 5)]
    assert device.repeat_calls == [True]


def test_client_repeat_is_refused_off_an_allowed_frequency() -> None:
    """MeshTerm refuses client repeat on a frequency that is not allowed.

    The firmware relays only on its allowed frequencies. It is always possible to turn
    the relaying off.
    """
    snapshot = {"radio_freq": 869.618, "repeat_freqs": [(433.0, 433.0), (869.495, 869.495)]}
    with pytest.raises(DeviceConfigError, match="relays only on 433, 869.495 MHz"):
        parse_value(get_spec("client_repeat"), "on", snapshot)
    assert parse_value(get_spec("client_repeat"), "off", snapshot) is False


# -- the auto-add hop limit: the byte after the bitmask -----------------------------------


def test_the_auto_add_hop_limit_restates_the_bitmask() -> None:
    """The auto-add hop limit states the bitmask again.

    The limit goes after the bitmask. Thus when MeshTerm sets the limit, it sends the
    bitmask again, unchanged.
    """

    class _AutoAdd:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def set_autoadd_config(self, flags, max_hops=None) -> None:
            self.calls.append((flags, max_hops))

    device = _AutoAdd()
    asyncio.run(_apply_all(device, {"autoadd_config": 0x1E}, [("autoadd_max_hops", "3")]))
    assert device.calls == [(0x1E, 3)]


def test_the_hop_limit_is_read_from_the_frame_the_library_half_parses() -> None:
    """The code reads the hop limit from the frame that the library only half parses.

    The library keeps the first byte of the reply. The code takes the second byte from the
    raw frame.
    """
    from types import SimpleNamespace

    from meshterm.core.connection import MeshCoreDevice

    class _Reader:
        async def handle_rx(self, data) -> None:
            self.last = bytes(data)

    reader = _Reader()

    class _Commands:
        async def get_autoadd_config(self):
            # This is what a transport does with a frame: it gives the frame to the reader,
            # by attribute.
            await reader.handle_rx(bytearray([25, 0x1E, 3]))
            return SimpleNamespace(payload={"config": 0x1E}, is_error=lambda: False)

    device = object.__new__(MeshCoreDevice)
    device._mc = SimpleNamespace(_reader=reader, commands=_Commands())

    async def read() -> tuple:
        return await device.get_autoadd_config(), await device.get_autoadd_max_hops()

    assert asyncio.run(read()) == (0x1E, 3)
    assert reader.last == bytes([25, 0x1E, 3])  # the library still saw its frame
    assert "handle_rx" not in vars(reader)  # and the tap is removed again


def test_a_negative_tx_power_reads_back_signed() -> None:
    """A negative TX power reads back with its sign.

    The firmware accepts -9 dBm. It reports a signed byte, but the library reads the byte
    as unsigned.
    """
    from types import SimpleNamespace

    from meshterm.core.connection import MeshCoreDevice

    class _Commands:
        async def send_appstart(self):
            return SimpleNamespace(payload={"tx_power": 247})

    device = object.__new__(MeshCoreDevice)
    device._mc = SimpleNamespace(commands=_Commands())
    assert asyncio.run(device.get_self_info())["tx_power"] == -9
