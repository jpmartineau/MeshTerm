# SPDX-License-Identifier: Apache-2.0
"""Tests for device discovery, the remembered-device store, and selection.

All run without hardware: serial enumeration is monkeypatched, and the store/selection
logic is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from meshterm.core.config import DeviceProfile
from meshterm.core.device_store import DeviceStore
from meshterm.core.discovery import DiscoveredDevice, discover_devices
from meshterm.core.selection import DeviceSelectionError, resolve_device


@dataclass
class FakePortInfo:
    """Stand-in for ``serial.tools.list_ports_common.ListPortInfo``."""

    device: str
    description: str | None = None
    hwid: str | None = None
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None


def _patch_ports(monkeypatch: pytest.MonkeyPatch, ports: list[FakePortInfo]) -> None:
    """Make ``discover_devices`` see exactly ``ports``."""
    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", lambda: list(ports))


# -- discovery -----------------------------------------------------------------


def test_discover_maps_fields_and_flags_lora(monkeypatch: pytest.MonkeyPatch) -> None:
    """USB metadata is mapped through and known vendors are flagged as likely LoRa."""
    _patch_ports(
        monkeypatch,
        [
            FakePortInfo(
                device="COM5",
                description="USB Serial",
                vid=0x303A,  # Espressif → likely LoRa
                pid=0x1001,
                serial_number="ABC123",
                product="Wio SX1262",
            )
        ],
    )
    [dev] = discover_devices()
    assert dev.port == "COM5"
    assert dev.product == "Wio SX1262"
    assert dev.is_likely_lora
    assert dev.vendor_label == "Espressif"
    assert "COM5" in dev.label


def test_discover_sorts_likely_lora_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Likely-LoRa devices sort ahead of unrecognized adapters."""
    _patch_ports(
        monkeypatch,
        [
            FakePortInfo(device="COM9", vid=0x1234, pid=0x0001),  # unknown vendor
            FakePortInfo(device="COM3", vid=0x10C4, pid=0xEA60),  # Silabs → LoRa
        ],
    )
    devices = discover_devices()
    assert [d.port for d in devices] == ["COM3", "COM9"]
    assert devices[0].is_likely_lora and not devices[1].is_likely_lora


def test_confidence_tiers_distinguish_boards_from_bridges() -> None:
    """A native-USB board is 'board'; a bare UART bridge is only 'bridge'; else 'unknown'."""
    board = DiscoveredDevice("COM5", vid=0x303A, pid=0x1001)  # Espressif native USB
    bridge = DiscoveredDevice("COM6", vid=0x10C4, pid=0xEA60)  # CP210x UART bridge
    unknown = DiscoveredDevice("COM7", vid=0x1234, pid=0x0001)
    assert board.confidence == "board" and board.is_likely_lora
    assert bridge.confidence == "bridge" and bridge.is_likely_lora
    assert unknown.confidence == "unknown" and not unknown.is_likely_lora


def test_sort_orders_boards_then_bridges_then_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery sorts LoRa boards ahead of bare serial bridges, and both ahead of unknown."""
    _patch_ports(
        monkeypatch,
        [
            FakePortInfo(device="COM9", vid=0x1234, pid=0x0001),  # unknown
            FakePortInfo(device="COM3", vid=0x10C4, pid=0xEA60),  # bridge
            FakePortInfo(device="COM1", vid=0x303A, pid=0x1001),  # board
        ],
    )
    assert [d.port for d in discover_devices()] == ["COM1", "COM3", "COM9"]


def test_label_does_not_repeat_the_port() -> None:
    """A Windows description already ending in '(COM11)' is not suffixed with it again."""
    dev = DiscoveredDevice("COM11", description="USB Serial Device (COM11)")
    assert dev.label == "USB Serial Device (COM11)"
    # A product name without the port still gets one appended.
    assert DiscoveredDevice("COM5", product="Wio SX1262").label == "Wio SX1262 (COM5)"


def test_mock_device_label_has_no_empty_port_suffix() -> None:
    """The simulator has no serial port to append to its description."""
    dev = DiscoveredDevice(
        transport="mock", description="the built-in simulator (nothing transmits)"
    )
    assert dev.label == "the built-in simulator (nothing transmits)"


def test_stable_id_precedence() -> None:
    """stable_id prefers serial number, then vid:pid, then the port name."""
    assert DiscoveredDevice("COM5", serial_number="SN1", vid=1, pid=2).stable_id == "sn:SN1"
    assert DiscoveredDevice("COM5", vid=0x303A, pid=0x1001).stable_id == "vidpid:303a:1001"
    assert DiscoveredDevice("COM5").stable_id == "port:COM5"


# -- BLE transport -------------------------------------------------------------


def _ble(address: str = "AA:BB:CC:DD:EE:FF", name: str = "MeshCore-Base") -> DiscoveredDevice:
    """Build a BLE DiscoveredDevice as the scanner would."""
    return DiscoveredDevice(
        transport="ble", address=address, name=name, description=name, product=name
    )


def test_ble_device_identity_and_labels() -> None:
    """A BLE device reports its address as the target, a stable ble: id, and a BLE label."""
    dev = _ble()
    assert dev.is_ble
    assert dev.target == "AA:BB:CC:DD:EE:FF"  # the connection identifier is the address
    assert dev.stable_id == "ble:aa:bb:cc:dd:ee:ff"  # stable across sessions, case-folded
    assert dev.confidence == "board" and dev.is_likely_lora  # a MeshCore advert is confident
    # The VENDOR column is *just* the hardware maker: a BLE advert rarely carries one, so it
    # stays blank rather than mislabelling the transport ("Bluetooth") as a vendor. The
    # transport is shown in its own TYPE column on the picker instead.
    assert dev.vendor_label == ""
    assert dev.label == "MeshCore-Base (BLE)"


def test_ble_vendor_label_uses_manufacturer_when_present() -> None:
    """A BLE advert that does expose a manufacturer string reports it as the vendor."""
    dev = DiscoveredDevice(transport="ble", address="AA:BB", name="MeshCore", manufacturer="Heltec")
    assert dev.vendor_label == "Heltec"


def test_ble_and_serial_stable_ids_never_collide() -> None:
    """A BLE address and a serial number/port can't map to the same remembered device."""
    assert _ble().stable_id != DiscoveredDevice("COM5", serial_number="SN1").stable_id


# -- TCP transport -------------------------------------------------------------


def test_tcp_device_identity_and_labels() -> None:
    """A TCP device reports host:port as its target, a stable tcp: id, and a network label."""
    from meshterm.core.discovery import tcp_device

    dev = tcp_device("192.168.1.50", 5000, name="WifiNode")
    assert dev.is_tcp and dev.transport == "tcp"
    assert dev.target == "192.168.1.50:5000"  # the connection identifier is host:port
    assert dev.stable_id == "tcp:192.168.1.50:5000"
    assert dev.confidence == "board" and dev.is_likely_lora  # a named endpoint is confident
    assert dev.vendor_label == ""  # a network endpoint exposes no maker
    assert dev.label == "WifiNode (192.168.1.50:5000)"


def test_tcp_stable_ids_never_collide_with_other_transports() -> None:
    """A TCP endpoint can't map to the same remembered device as a BLE/serial one."""
    from meshterm.core.discovery import tcp_device

    tcp = tcp_device("10.0.0.5", 5000)
    assert tcp.stable_id != _ble().stable_id
    assert tcp.stable_id != DiscoveredDevice("COM5", serial_number="SN1").stable_id


def test_parse_tcp_endpoint_forms() -> None:
    """A bare host takes the default port; host:port and bracketed IPv6 parse explicitly."""
    from meshterm.core.discovery import DEFAULT_TCP_PORT, parse_tcp_endpoint

    assert parse_tcp_endpoint("192.168.1.50") == ("192.168.1.50", DEFAULT_TCP_PORT)
    assert parse_tcp_endpoint("meshcore.local:6000") == ("meshcore.local", 6000)
    assert parse_tcp_endpoint("[::1]:5000") == ("::1", 5000)
    assert parse_tcp_endpoint("  10.0.0.5 : 7000 ".replace(" ", "")) == ("10.0.0.5", 7000)


def test_parse_tcp_endpoint_rejects_bad_values() -> None:
    """An empty host or a non-numeric / out-of-range port raises a user-facing error."""
    from meshterm.core.discovery import parse_tcp_endpoint

    for bad in ("", "   ", "host:notaport", "host:0", "host:70000"):
        with pytest.raises(ValueError):
            parse_tcp_endpoint(bad)


def test_tcp_device_store_round_trip(tmp_path: Path) -> None:
    """A remembered TCP device persists its transport, host, and port, matched by stable_id."""
    from meshterm.core.discovery import tcp_device

    store = DeviceStore(tmp_path / "devices.json")
    dev = tcp_device("192.168.1.50", 5000, name="WifiNode")
    store.remember(dev, node_name="WifiNode")

    loaded = store.load()
    assert loaded is not None
    assert loaded.is_tcp and loaded.transport == "tcp"
    assert loaded.host == "192.168.1.50" and loaded.tcp_port == 5000
    assert loaded.target == "192.168.1.50:5000"
    assert loaded.node_name == "WifiNode"
    assert loaded.matches(dev)


async def test_discover_ble_filters_to_meshcore(monkeypatch: pytest.MonkeyPatch) -> None:
    """The BLE scan keeps only MeshCore-named adverts and maps them to devices."""
    from types import SimpleNamespace

    import meshterm.core.discovery as discovery

    class _FakeScanner:
        @staticmethod
        async def discover(timeout: float, return_adv: bool):
            return {
                "AA:BB:CC:DD:EE:FF": (
                    SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="MeshCore-Base"),
                    SimpleNamespace(local_name="MeshCore-Base", rssi=-60),
                ),
                "11:22:33:44:55:66": (  # a random unrelated Bluetooth gadget — filtered out
                    SimpleNamespace(address="11:22:33:44:55:66", name="AirPods"),
                    SimpleNamespace(local_name="AirPods", rssi=-70),
                ),
            }

    monkeypatch.setattr(discovery, "BleakScanner", _FakeScanner, raising=False)
    # ``discover_ble_devices`` imports BleakScanner from bleak inside the function; patch there.
    import bleak

    monkeypatch.setattr(bleak, "BleakScanner", _FakeScanner, raising=False)

    devices = await discovery.discover_ble_devices(timeout=0.0)
    assert [d.name for d in devices] == ["MeshCore-Base"]
    assert devices[0].is_ble and devices[0].address == "AA:BB:CC:DD:EE:FF"
    # The scan's live BLEDevice rides along so the connect can open the peripheral directly
    # instead of re-discovering the address (the flaky path on Windows).
    assert devices[0].ble_device is not None
    assert devices[0].ble_device.address == "AA:BB:CC:DD:EE:FF"


async def test_discover_ble_survives_no_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scan failure (no adapter / Bluetooth off) yields an empty list, never raises."""
    import bleak

    import meshterm.core.discovery as discovery

    class _BoomScanner:
        @staticmethod
        async def discover(timeout: float, return_adv: bool):
            raise OSError("no Bluetooth adapter")

    monkeypatch.setattr(bleak, "BleakScanner", _BoomScanner, raising=False)
    assert await discovery.discover_ble_devices(timeout=0.0) == []
    # A hiccup is not something the reader can act on, so it leaves no reason behind.
    assert discovery.ble_unavailable_reason() is None


async def test_discover_ble_records_a_refusal_the_user_can_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied scan keeps bleak's sentence instead of reading as "nothing nearby".

    On macOS this is the routine case, not an exotic one: Bluetooth is granted to the
    terminal running MeshTerm, so an ungranted terminal — and every SSH session, which
    macOS refuses to even prompt in — scans successfully and hears nothing at all.
    """
    import bleak

    import meshterm.core.discovery as discovery

    not_available = pytest.importorskip("bleak.exc").BleakBluetoothNotAvailableError

    class _DeniedScanner:
        @staticmethod
        async def discover(timeout: float, return_adv: bool):
            raise not_available("Bluetooth access is denied by the user", 2)

    monkeypatch.setattr(bleak, "BleakScanner", _DeniedScanner, raising=False)
    assert await discovery.discover_ble_devices(timeout=0.0) == []
    reason = discovery.ble_unavailable_reason()
    assert reason is not None and "denied" in reason

    # And it describes the latest attempt, never a stale one.
    monkeypatch.setattr(bleak, "BleakScanner", _empty_scanner(), raising=False)
    assert await discovery.discover_ble_devices(timeout=0.0) == []
    assert discovery.ble_unavailable_reason() is None


def _empty_scanner():
    """A scanner that works and simply hears nothing — the "not a fault" case."""

    class _Quiet:
        @staticmethod
        async def discover(timeout: float, return_adv: bool):
            return {}

    return _Quiet


# -- device store --------------------------------------------------------------


def test_device_store_round_trip(tmp_path: Path) -> None:
    """A remembered device writes and reads back, matched by stable_id."""
    store = DeviceStore(tmp_path / "devices.json")
    assert store.load() is None  # nothing remembered yet

    dev = DiscoveredDevice("COM5", serial_number="SN1", product="Wio SX1262")
    store.remember(dev, node_name="BaseStation")

    loaded = store.load()
    assert loaded is not None
    assert loaded.stable_id == "sn:SN1"
    assert loaded.node_name == "BaseStation"  # the mesh name learned on connect
    assert loaded.matches(dev)
    assert not loaded.matches(DiscoveredDevice("COM6", serial_number="OTHER"))


def test_remember_keeps_known_node_name_when_none_supplied(tmp_path: Path) -> None:
    """A later connect without a node name preserves the previously remembered one."""
    store = DeviceStore(tmp_path / "devices.json")
    dev = DiscoveredDevice("COM5", serial_number="SN1")
    store.remember(dev, node_name="BaseStation")

    store.remember(dev)  # e.g. the identity probe failed this time
    loaded = store.load()
    assert loaded is not None and loaded.node_name == "BaseStation"


def test_device_store_round_trips_hardware_model(tmp_path: Path) -> None:
    """The firmware model learned at connect time writes and reads back."""
    store = DeviceStore(tmp_path / "devices.json")
    dev = DiscoveredDevice(transport="ble", address="AA:BB:CC:DD:EE:FF", name="MeshCore-Testbench")
    store.remember(dev, node_name="Waymarker", hardware_model="Seeed Tracker T1000-E")

    loaded = store.load()
    assert loaded is not None
    assert loaded.hardware_model == "Seeed Tracker T1000-E"  # the model the Hardware column shows


def test_remember_keeps_known_model_when_none_supplied(tmp_path: Path) -> None:
    """A reconnect on firmware that can't answer the query keeps the earlier model."""
    store = DeviceStore(tmp_path / "devices.json")
    dev = DiscoveredDevice("COM5", serial_number="SN1")
    store.remember(dev, hardware_model="Seeed Tracker T1000-E")

    store.remember(dev, node_name="Base")  # a later connect that learned no model
    loaded = store.load()
    assert loaded is not None
    assert loaded.node_name == "Base"
    assert loaded.hardware_model == "Seeed Tracker T1000-E"  # not erased


def test_device_store_tolerates_corrupt_file(tmp_path: Path) -> None:
    """A corrupt state file is treated as 'nothing remembered'."""
    path = tmp_path / "devices.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert DeviceStore(path).load() is None


def test_device_store_remembers_every_confirmed_device(tmp_path: Path) -> None:
    """Confirmed devices are all kept; ``load`` returns the most recently connected one."""
    store = DeviceStore(tmp_path / "devices.json")
    first = DiscoveredDevice("COM5", serial_number="SN1", product="Wio")
    second = DiscoveredDevice("COM6", serial_number="SN2", product="Heltec")

    store.remember(first, node_name="Base")
    store.remember(second, node_name="Roamer")

    registry = store.load_all()
    assert set(registry) == {"sn:SN1", "sn:SN2"}  # both remembered forever
    assert store.is_known(first) and store.is_known(second)
    assert not store.is_known(DiscoveredDevice("COM7", serial_number="SN3"))

    last = store.load()
    assert last is not None and last.stable_id == "sn:SN2"  # most recent is the default

    # Re-confirming the first makes it the default again without dropping the second.
    store.remember(first)
    assert store.load().stable_id == "sn:SN1"
    assert set(store.load_all()) == {"sn:SN1", "sn:SN2"}


def test_device_store_forget_removes_and_reassigns_default(tmp_path: Path) -> None:
    """``forget`` drops a record and hands the default to the newest survivor (or clears it)."""
    store = DeviceStore(tmp_path / "devices.json")
    first = DiscoveredDevice("COM5", serial_number="SN1", product="Wio")
    second = DiscoveredDevice("COM6", serial_number="SN2", product="Heltec")
    store.remember(first, node_name="Base")
    store.remember(second, node_name="Roamer")  # the newer default

    # Forgetting the current default reassigns it to the remaining (older) device.
    assert store.forget("sn:SN2") is True
    assert set(store.load_all()) == {"sn:SN1"}
    assert store.load().stable_id == "sn:SN1"

    # Forgetting an unknown id is a no-op that reports it did nothing.
    assert store.forget("sn:absent") is False

    # Forgetting the last device empties the registry and clears the default.
    assert store.forget("sn:SN1") is True
    assert store.load_all() == {}
    assert store.load() is None


def test_device_store_migrates_old_flat_format(tmp_path: Path) -> None:
    """A pre-registry flat record still reads back as a one-entry registry."""
    path = tmp_path / "devices.json"
    path.write_text(
        '{"stable_id": "sn:SN1", "port": "COM5", "label": "Wio (COM5)", '
        '"last_connected": "2025-01-01T00:00:00", "node_name": "Base"}',
        encoding="utf-8",
    )
    store = DeviceStore(path)
    loaded = store.load()
    assert loaded is not None and loaded.stable_id == "sn:SN1"
    assert loaded.node_name == "Base"
    assert set(store.load_all()) == {"sn:SN1"}


# -- selection -----------------------------------------------------------------


def test_resolve_prefers_explicit_port() -> None:
    """An explicit --port wins over everything else."""
    devices = [DiscoveredDevice("COM3", serial_number="SN1")]
    res = resolve_device(devices, None, explicit_port="COM9")
    assert res.port == "COM9" and res.source == "port"


def test_resolve_uses_profile_port() -> None:
    """A profile with a port is used when no --port is given."""
    profile = DeviceProfile(name="yagi", port="COM6")
    res = resolve_device([], None, profile=profile)
    assert res.port == "COM6" and res.source == "profile"


def test_resolve_uses_remembered_when_present() -> None:
    """The remembered default is used when that device is still attached."""
    dev = DiscoveredDevice("COM3", serial_number="SN1")
    store_dev = DiscoveredDevice("COM7", serial_number="SN1")  # same hardware, new port
    store = _remember(store_dev)
    res = resolve_device([dev], store, profile=None)
    assert res.port == "COM3" and res.source == "remembered"
    assert res.device is dev


def test_resolve_single_device_auto() -> None:
    """With exactly one device and no default, it is chosen automatically."""
    dev = DiscoveredDevice("COM3", serial_number="SN1")
    res = resolve_device([dev], None)
    assert res.port == "COM3" and res.source == "only"


def test_resolve_prefers_explicit_ble() -> None:
    """An explicit --ble selects the BLE transport and wins over everything else."""
    res = resolve_device([], None, explicit_ble="AA:BB:CC:DD:EE:FF")
    assert res.target == "AA:BB:CC:DD:EE:FF"
    assert res.transport == "ble" and res.source == "ble"


def test_resolve_single_ble_device_auto() -> None:
    """A lone in-range BLE companion is chosen automatically, transport and all."""
    dev = _ble()
    res = resolve_device([dev], None)
    assert res.target == dev.address and res.transport == "ble" and res.source == "only"


def test_resolve_uses_remembered_ble_device() -> None:
    """A remembered BLE default reconnects by address when it's back in range."""
    from meshterm.core.device_store import RememberedDevice

    dev = _ble()
    remembered = RememberedDevice(
        stable_id=dev.stable_id,
        port="",
        label=dev.label,
        last_connected="",
        transport="ble",
        address=dev.address,
    )
    res = resolve_device([dev], remembered)
    assert res.transport == "ble" and res.target == dev.address and res.source == "remembered"


def test_ble_device_store_round_trip(tmp_path: Path) -> None:
    """A remembered BLE device persists its transport and address, matched by stable_id."""
    store = DeviceStore(tmp_path / "devices.json")
    dev = _ble(name="MeshCore-Roamer")
    store.remember(dev, node_name="Roamer")

    loaded = store.load()
    assert loaded is not None
    assert loaded.is_ble and loaded.transport == "ble"
    assert loaded.address == dev.address and loaded.target == dev.address
    assert loaded.node_name == "Roamer"
    assert loaded.matches(dev)


def test_resolve_prefers_explicit_tcp() -> None:
    """An explicit --tcp selects the TCP transport, normalizing a bare host's default port."""
    res = resolve_device([], None, explicit_tcp="192.168.1.50")
    assert res.transport == "tcp" and res.source == "tcp"
    assert res.target == "192.168.1.50:5000"  # default port applied
    assert res.device is not None and res.device.stable_id == "tcp:192.168.1.50:5000"


def test_resolve_tcp_wins_over_ble_and_port() -> None:
    """--tcp outranks --ble and --port when more than one is (somehow) supplied."""
    res = resolve_device(
        [], None, explicit_tcp="10.0.0.5:5000", explicit_ble="AA:BB", explicit_port="COM5"
    )
    assert res.transport == "tcp" and res.target == "10.0.0.5:5000"


def test_resolve_uses_tcp_profile() -> None:
    """A TCP profile's host:port is used when no explicit override is given."""
    profile = DeviceProfile(name="wifi", transport="tcp", host="10.0.0.9", tcp_port=6000)
    res = resolve_device([], None, profile=profile)
    assert res.transport == "tcp" and res.source == "profile"
    assert res.target == "10.0.0.9:6000"


def test_resolve_bad_tcp_endpoint_raises() -> None:
    """A malformed --tcp value raises a clean selection error rather than a raw ValueError."""
    with pytest.raises(DeviceSelectionError):
        resolve_device([], None, explicit_tcp="host:notaport")


def test_resolve_ambiguous_raises(tmp_path: Path) -> None:
    """Two plausible companions with no usable default raise a guidance error."""
    devices = [
        DiscoveredDevice("COM3", vid=0x303A, pid=0x1001, serial_number="SN1"),
        DiscoveredDevice("COM4", vid=0x10C4, pid=0xEA60, serial_number="SN2"),
    ]
    with pytest.raises(DeviceSelectionError, match="Multiple companion devices"):
        resolve_device(devices, None)


def test_resolve_ignores_implausible_ports_when_one_board_is_present() -> None:
    """One real board among ports that look like nothing still resolves to the board.

    This is the macOS case: every Mac permanently presents two virtual ``/dev/cu.*``
    ports with no USB VID/PID, so counting them meant a Mac with a single companion
    attached saw three devices and refused to choose — auto-detection could never fire
    on that platform.
    """
    board = DiscoveredDevice("/dev/cu.usbmodem1101", vid=0x303A, pid=0x1001)
    devices = [
        board,
        DiscoveredDevice("/dev/cu.Bluetooth-Incoming-Port"),
        DiscoveredDevice("/dev/cu.debug-console"),
    ]
    res = resolve_device(devices, None)
    assert res.target == board.target and res.source == "only"


def test_resolve_lone_unrecognized_device_still_resolves() -> None:
    """A single adapter we can't place still connects — an unlisted VID is usually real."""
    lone = DiscoveredDevice("COM9", vid=0x1234, pid=0x0001)
    assert not lone.is_likely_lora
    res = resolve_device([lone], None)
    assert res.target == "COM9" and res.source == "only"


def test_resolve_several_implausible_ports_does_not_call_them_companions() -> None:
    """Ports that look like nothing are not reported as "multiple companion devices".

    A bare Mac with no companion attached reaches here with its two virtual ports, and
    being told it has several companions — and asked to pick one of them — is the least
    true answer available.
    """
    devices = [
        DiscoveredDevice("/dev/cu.Bluetooth-Incoming-Port"),
        DiscoveredDevice("/dev/cu.debug-console"),
    ]
    with pytest.raises(DeviceSelectionError, match="No companion devices detected among") as err:
        resolve_device(devices, None)
    assert "Multiple companion devices" not in str(err.value)


def test_resolve_no_devices_raises() -> None:
    """No devices at all raises a clear error mentioning --mock."""
    with pytest.raises(DeviceSelectionError, match="No companion devices"):
        resolve_device([], None)


def _remember(device: DiscoveredDevice):
    """Build a RememberedDevice for ``device`` without touching disk."""
    from meshterm.core.device_store import RememberedDevice

    return RememberedDevice(
        stable_id=device.stable_id, port=device.port, label=device.label, last_connected=""
    )
