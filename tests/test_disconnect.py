# SPDX-License-Identifier: Apache-2.0
"""Tests for graceful handling of a lost device connection.

Covers the exception classifier that distinguishes a dropped serial link from an ordinary
command failure, and :meth:`AppContext.reconnect`, which rebuilds the connection and restores
the services (event hub, passive monitor, chat) that were running before the drop. All run
against the :class:`MockDevice` simulator; no hardware required.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from meshterm import context as context_module
from meshterm.context import AppContext
from meshterm.core import connection, discovery
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.connection import DeviceCommandError, is_connection_lost
from meshterm.core.device_store import DeviceStore
from meshterm.core.discovery import DiscoveredDevice
from meshterm.persistence.repository import Repository
from meshterm.ui import menu


class _SerialException(Exception):
    """A stand-in for ``serial.SerialException`` (matched by type name, not import)."""


class _FakeSerialDevice:
    """A minimal stand-in for a connected serial :class:`Device` in liveness tests.

    Its :meth:`link_present` mirrors the real serial device: it reports whether the port is
    still enumerated by the OS, reading through the (monkeypatched) module function so tests
    can flip presence on and off.
    """

    transport = "serial"

    def __init__(self, port: str = "COM_TEST") -> None:
        self._port = port

    async def link_present(self) -> bool:
        return connection.serial_port_present(self._port)


def test_is_connection_lost_matches_serial_exception() -> None:
    """A pyserial-style read/write failure is classified as a dropped link."""
    assert is_connection_lost(_SerialException("ClearCommError failed (Access is denied.)"))


def test_is_connection_lost_walks_the_exception_chain() -> None:
    """A link error wrapped in a higher-level error is still detected via its cause."""
    try:
        try:
            raise _SerialException("WriteFile failed")
        except Exception as cause:
            raise RuntimeError("device write failed") from cause
    except Exception as exc:
        assert is_connection_lost(exc)


def test_is_connection_lost_matches_os_level_drops() -> None:
    """OS-layer connection teardown and telltale I/O messages count as a lost link."""
    assert is_connection_lost(ConnectionResetError("reset"))
    assert is_connection_lost(OSError("input/output error"))


def test_is_connection_lost_ignores_ordinary_failures() -> None:
    """A transient command timeout or a generic error is not a dropped link."""
    assert not is_connection_lost(DeviceCommandError("the companion didn't respond in time"))
    assert not is_connection_lost(ValueError("bad value"))


# These stand-ins are named to match bleak's real classes, since the classifier matches by
# type name (not import) — see ``_CONNECTION_LOST_TYPES``.
class BleakError(Exception):  # noqa: N818 - mirrors bleak's own (non-Error-suffixed) name
    """A stand-in for ``bleak.exc.BleakError`` (matched by type name, not import)."""


class BleakDeviceNotFoundError(Exception):
    """A stand-in for ``bleak.exc.BleakDeviceNotFoundError``."""


def test_is_connection_lost_matches_ble_errors() -> None:
    """A dropped Bluetooth link is classified as a lost connection, like a serial unplug."""
    assert is_connection_lost(BleakError("gatt operation failed"))  # matched by type name
    assert is_connection_lost(BleakDeviceNotFoundError("AA:BB:CC not found"))
    # The meshcore BLE transport reports link loss via a callback reason string.
    assert is_connection_lost(RuntimeError("ble_transport_lost"))


class BleakGATTProtocolError(Exception):
    """Stand-in for bleak's GATT auth rejection (classifier matches its message, not import)."""


def test_ble_auth_error_is_recognized_and_is_not_a_lost_link() -> None:
    """A PIN/pairing rejection is its own actionable case — never mistaken for a dropped link."""
    exc = BleakGATTProtocolError("(5, 'GATT Protocol Error: Insufficient Authentication')")
    assert connection._is_ble_auth_error(exc)
    assert not is_connection_lost(exc)  # so the session offers a PIN, not a reconnect


def test_ble_auth_error_walks_the_exception_chain() -> None:
    """An auth rejection wrapped by the meshcore transport is still recognized via its cause."""
    try:
        try:
            raise BleakGATTProtocolError("Insufficient Encryption")
        except Exception as cause:
            raise RuntimeError("connect failed") from cause
    except Exception as exc:
        assert connection._is_ble_auth_error(exc)


#: Stand-in Bluetooth addresses. Never a real companion's: a test that names one turns a
#: personal device into repository content, and any hardware coupling here would be a lie —
#: nothing in this file touches a radio.
_BONDED_ADDR = "00:11:22:33:44:55"
_OPEN_ADDR = "AA:BB:CC:DD:EE:FF"
#: A stand-in pairing code, for the same reason. What matters is only that one was supplied.
_A_PIN = "123456"


async def _disable_windows_pairing(dev: connection.MeshCoreDevice) -> None:
    """Stub out the WinRT ProvidePin step so ``_create_ble`` stays hermetic in tests.

    On a real Windows host ``_pair_ble_windows`` would reach the OS Bluetooth stack (and the
    physical device); pinning it to a no-op reproduces the non-Windows / no-winrt path so the
    auth-translation logic can be exercised without hardware.
    """

    async def _never_pairs(*, force: bool) -> bool:
        return False

    async def _no_bond() -> bool:
        return False

    dev._pair_ble_windows = _never_pairs  # type: ignore[method-assign]
    dev._ble_os_bonded = _no_bond  # type: ignore[method-assign]


def _fake_ble_stack(monkeypatch: pytest.MonkeyPatch, *outcomes):
    """A stand-in ``MeshCore`` class, scripted one entry per connect attempt.

    MeshTerm builds the meshcore client itself (:meth:`~MeshCoreDevice._connect_owned_ble`)
    rather than letting ``create_ble`` construct and then orphan it, so the fake here is a
    *class* that gets instantiated per attempt — and each instance records whether it was
    closed, which is the property that matters: a connect that fails must never leave its
    link open, or the peripheral goes on believing it has a peer and stops advertising.

    Args:
        monkeypatch: Used to stub the lazily-imported ``meshcore`` module, so no real
            ``BLEConnection`` (and therefore no ``bleak``) is needed.
        outcomes: What each successive ``connect()`` does — an exception instance to raise,
            or a value to return (``None`` standing for an unanswered handshake).

    Returns:
        The fake class; its ``built`` list holds the instances it made, in order.
    """
    import sys
    import types

    class _FakeBLEConnection:
        def __init__(self, address=None, device=None, pin=None) -> None:
            self.address, self.device, self.pin = address, device, pin

    class _FakeMeshCore:
        built: list = []
        script: list = list(outcomes)

        def __init__(self, cx, **kwargs) -> None:
            self.cx = cx
            self.kwargs = kwargs
            self.disconnect_calls = 0
            type(self).built.append(self)

        async def connect(self):
            outcome = type(self).script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

    module = types.ModuleType("meshcore")
    module.BLEConnection = _FakeBLEConnection
    module.MeshCore = _FakeMeshCore
    monkeypatch.setitem(sys.modules, "meshcore", module)
    return _FakeMeshCore


async def test_create_ble_translates_auth_error_to_pin_guidance(monkeypatch) -> None:
    """A raw GATT auth rejection becomes a DeviceAuthenticationError that names the PIN fix."""
    # Pinned off macOS: there the same rejection starts an OS-run pairing and is retried
    # rather than reported, and the advice is the system dialog rather than --ble-pin.
    # Without this the test would assert Windows/Linux wording on the macOS CI runner.
    monkeypatch.setattr(sys, "platform", "win32")
    # No PIN supplied -> tell the user to pass one. The subclass lets the interactive picker
    # catch "needs a PIN" specifically, while the CLI still catches it as DeviceCommandError.
    fake = _fake_ble_stack(monkeypatch, BleakGATTProtocolError("Insufficient Authentication"))
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    await _disable_windows_pairing(dev)
    with pytest.raises(connection.DeviceAuthenticationError) as excinfo:
        await dev._create_ble(fake)
    assert isinstance(excinfo.value, DeviceCommandError)  # so the scripted CLI catches it too
    assert "--ble-pin" in str(excinfo.value)
    assert fake.built[0].disconnect_calls == 1  # the doomed link was released, not stranded

    # PIN supplied but rejected -> say it was wrong, not that none was given.
    fake_pin = _fake_ble_stack(monkeypatch, BleakGATTProtocolError("Insufficient Authentication"))
    dev_pin = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR, pin=_A_PIN)
    await _disable_windows_pairing(dev_pin)
    with pytest.raises(connection.DeviceAuthenticationError) as excinfo_pin:
        await dev_pin._create_ble(fake_pin)
    assert "rejected" in str(excinfo_pin.value).lower()


async def test_create_ble_names_a_stale_windows_bond(monkeypatch) -> None:
    """No PIN, but Windows holds a bond the device refuses: say the bond is stale.

    The case reported from the field — a companion Windows showed as paired, refusing the
    subscribe after the device's side of the bond was lost. "Requires a PIN" was the wrong
    story for someone looking at "Paired" in Settings.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    fake = _fake_ble_stack(monkeypatch, BleakGATTProtocolError("Insufficient Authentication"))
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    await _disable_windows_pairing(dev)

    async def _bonded() -> bool:
        return True

    dev._ble_os_bonded = _bonded  # type: ignore[method-assign]
    with pytest.raises(connection.DeviceAuthenticationError) as excinfo:
        await dev._create_ble(fake)
    assert "Windows has" in str(excinfo.value)
    assert "--ble-pin" in str(excinfo.value)  # the PIN is still how MeshTerm re-pairs it
    assert "saved pairing" in excinfo.value.hint  # and the PIN dialog says why it is asking


@pytest.mark.parametrize(
    ("pairing", "expected", "hint"),
    [
        (connection._BlePairing("failed", "authentication failure"), "rejected", "PIN"),
        (connection._BlePairing("failed", "connection rejected"), "refused to pair", "refused"),
        (connection._BlePairing("failed", "hardware failure"), "couldn't pair", "hardware"),
        (connection._BlePairing("absent"), "couldn't reach", "reach"),
        (connection._BlePairing("paired"), "still refuses", "still refused"),
        (None, "rejected the Bluetooth PIN", "PIN was rejected"),
    ],
)
async def test_create_ble_names_what_the_pairing_found(
    monkeypatch, pairing, expected: str, hint: str
) -> None:
    """With a PIN, the refusal names the pairing's own outcome, each with its own fix."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake = _fake_ble_stack(
        monkeypatch,
        BleakGATTProtocolError("Insufficient Authentication"),
        BleakGATTProtocolError("Insufficient Authentication"),
    )
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR, pin=_A_PIN)

    async def _pair(*, force: bool) -> bool:
        dev._ble_pairing = pairing
        return pairing is not None and pairing.outcome == "paired"

    dev._pair_ble_windows = _pair  # type: ignore[method-assign]
    with pytest.raises(connection.DeviceAuthenticationError) as excinfo:
        await dev._create_ble(fake)
    assert expected in str(excinfo.value)
    assert hint in excinfo.value.hint


def test_a_refused_pairing_step_is_an_auth_error() -> None:
    """A failing pair() in bleak is a pairing problem, not a device that "didn't answer"."""
    assert connection._is_ble_auth_error(Exception("Could not pair with device: 19: FAILED"))
    assert connection._is_ble_auth_error(Exception("org.bluez.Error.AuthenticationFailed"))


@pytest.mark.parametrize(
    ("stage", "expected"), [("connect", "took longer"), ("identity", "identity")]
)
async def test_ble_probe_timeout_names_its_stage(monkeypatch, stage: str, expected: str) -> None:
    """A BLE probe that runs out of time says which stage stalled, instead of returning None."""
    dev = connection.MeshCoreDevice(transport="ble", address=_OPEN_ADDR)

    async def _connect() -> None:
        if stage == "connect":
            raise asyncio.TimeoutError

    async def _self_info() -> dict:
        raise asyncio.TimeoutError

    dev.connect = _connect  # type: ignore[method-assign]
    dev.get_self_info = _self_info  # type: ignore[method-assign]
    with pytest.raises(DeviceCommandError) as excinfo:
        await connection._probe(dev, 1.0)
    assert expected in str(excinfo.value)


async def test_create_ble_repairs_stale_bond_and_retries_once(monkeypatch) -> None:
    """A first auth failure triggers one unpair-and-re-pair, then the retried connect succeeds.

    Models the Windows upgrade case: a leftover unauthenticated "Just Works" bond makes the
    first connect fail even with the right PIN, so ``_pair_ble_windows(force=True)`` clears it
    and the second connect goes through.
    """
    fake = _fake_ble_stack(
        monkeypatch, BleakGATTProtocolError("Insufficient Authentication"), "handshake-ok"
    )
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR, pin=_A_PIN)
    repairs: list[bool] = []

    async def _pair(*, force: bool) -> bool:
        repairs.append(force)
        return force  # the pre-connect pass (force=False) no-ops; the repair (force=True) works

    dev._pair_ble_windows = _pair  # type: ignore[method-assign]
    result = await dev._create_ble(fake)
    assert result is fake.built[1]  # the retry's client is what the caller gets
    assert len(fake.built) == 2  # failed once, retried once
    assert repairs == [False, True]  # pre-connect attempt, then the healing re-pair
    assert fake.built[0].disconnect_calls == 1  # ...and the failed one was closed first
    assert fake.built[1].disconnect_calls == 0  # the live one is handed over open


async def test_create_ble_gives_up_after_one_repair(monkeypatch) -> None:
    """A wrong PIN that never bonds fails cleanly rather than looping on the repair retry."""
    fake = _fake_ble_stack(
        monkeypatch,
        BleakGATTProtocolError("Insufficient Authentication"),
        BleakGATTProtocolError("Insufficient Authentication"),
    )
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR, pin=_A_PIN)
    calls: list[bool] = []

    async def _pair(*, force: bool) -> bool:
        calls.append(force)
        return force  # even the repair "succeeds" so we prove the retry runs exactly once

    dev._pair_ble_windows = _pair  # type: ignore[method-assign]
    with pytest.raises(connection.DeviceAuthenticationError):
        await dev._create_ble(fake)
    # force=False (pre-connect), then force=True (repair). The repair's retry passes
    # allow_repair=False, so there is no third pairing attempt even though it keeps failing.
    assert calls == [False, True]
    assert all(client.disconnect_calls == 1 for client in fake.built)


async def test_create_ble_retries_a_transient_link_failure(monkeypatch) -> None:
    """A transport-level ConnectionError is retried once, and the scanned BLEDevice rides along.

    Models the common Windows flake: the first link open misses the (slow-advertising)
    peripheral and the meshcore client raises a bare ``ConnectionError``; users learned to
    work around it by re-selecting the device — a manual retry — so the connect retries
    itself before surfacing the failure.
    """
    scanned_device = object()  # the BLEDevice the discovery scan produced
    fake = _fake_ble_stack(
        monkeypatch, ConnectionError("Failed to connect to device"), "handshake-ok"
    )
    monkeypatch.setattr(connection, "_BLE_CONNECT_RETRY_DELAY_S", 0.0)
    dev = connection.MeshCoreDevice(transport="ble", address=_OPEN_ADDR, ble_device=scanned_device)
    await _disable_windows_pairing(dev)
    assert await dev._create_ble(fake) is fake.built[1]
    assert len(fake.built) == 2  # failed once, retried once
    # Every attempt connects through the already-discovered BLEDevice, never a bare address.
    assert all(client.cx.device is scanned_device for client in fake.built)


async def test_create_ble_gives_up_after_the_retry(monkeypatch) -> None:
    """A link that never opens says so after the bounded retries — not "not a companion"."""
    fake = _fake_ble_stack(
        monkeypatch,
        ConnectionError("Failed to connect to device"),
        ConnectionError("Failed to connect to device"),
    )
    monkeypatch.setattr(connection, "_BLE_CONNECT_RETRY_DELAY_S", 0.0)
    dev = connection.MeshCoreDevice(transport="ble", address=_OPEN_ADDR)
    await _disable_windows_pairing(dev)
    with pytest.raises(DeviceCommandError) as excinfo:
        await dev._create_ble(fake)
    assert not isinstance(excinfo.value, connection.DeviceAuthenticationError)
    assert "couldn't open a Bluetooth link" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ConnectionError)  # the original rides along
    assert len(fake.built) == connection._BLE_CONNECT_ATTEMPTS
    assert all(client.disconnect_calls == 1 for client in fake.built)


async def test_create_ble_never_retries_an_auth_rejection(monkeypatch) -> None:
    """A PIN/bond rejection is translated on the first attempt — never looped by the retry."""
    # Off macOS, where a rejection is the *start* of an OS-run pairing and is waited on.
    monkeypatch.setattr(sys, "platform", "win32")
    fake = _fake_ble_stack(monkeypatch, BleakGATTProtocolError("Insufficient Authentication"))
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    await _disable_windows_pairing(dev)
    with pytest.raises(connection.DeviceAuthenticationError):
        await dev._create_ble(fake)
    assert len(fake.built) == 1


async def test_create_ble_waits_for_macos_to_finish_pairing(monkeypatch) -> None:
    """On macOS the first auth rejection is the pairing *starting*, so the connect waits.

    Touching the companion's authenticated characteristic is the only way to make
    CoreBluetooth pair at all, so the rejection and the Passkey dialog are the same event.
    Giving up there reported an error for a pairing that was succeeding, and the device
    connected only when the user selected it a second time.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(connection, "_BLE_MACOS_PAIRING_DELAY_S", 0)
    # Refused twice while the dialog is up, then the bond lands and the subscribe works.
    fake = _fake_ble_stack(
        monkeypatch,
        BleakGATTProtocolError("Insufficient Authentication"),
        BleakGATTProtocolError("Insufficient Authentication"),
        "handshake-ok",
    )
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    await _disable_windows_pairing(dev)

    result = await dev._create_ble(fake)
    assert result is fake.built[2]  # the attempt made after the bond is what the caller gets
    assert len(fake.built) == 3
    assert fake.built[0].disconnect_calls == 1  # each refused link was released, not stranded
    assert fake.built[1].disconnect_calls == 1
    assert fake.built[2].disconnect_calls == 0  # the live one is handed over open


async def test_create_ble_gives_up_when_macos_pairing_is_dismissed(monkeypatch) -> None:
    """A dialog nobody answers still fails in the end, and says where the code is asked for."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(connection, "_BLE_MACOS_PAIRING_DELAY_S", 0)
    refusals = [BleakGATTProtocolError("Insufficient Authentication")] * (
        connection._BLE_MACOS_PAIRING_ATTEMPTS + 1
    )
    fake = _fake_ble_stack(monkeypatch, *refusals)
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    await _disable_windows_pairing(dev)

    with pytest.raises(connection.DeviceAuthenticationError) as excinfo:
        await dev._create_ble(fake)
    assert len(fake.built) == connection._BLE_MACOS_PAIRING_ATTEMPTS + 1  # bounded, not endless
    # --ble-pin cannot help on macOS: the OS collects the code, so don't send the reader there.
    assert "--ble-pin" not in str(excinfo.value)
    assert "System Settings" in str(excinfo.value)


async def test_connect_reports_an_unanswered_handshake_and_closes_it(monkeypatch) -> None:
    """Transport up but no identity reply: a clean "not a companion", link still released."""
    fake = _fake_ble_stack(monkeypatch, None)
    dev = connection.MeshCoreDevice(transport="ble", address=_OPEN_ADDR)
    await _disable_windows_pairing(dev)
    with pytest.raises(DeviceCommandError):
        await dev.connect()
    assert fake.built[0].disconnect_calls == 1


async def test_pair_ble_windows_noops_without_pin() -> None:
    """Pairing is skipped (no WinRT touched) when no PIN is set — the fast, hermetic path."""
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    assert await dev._pair_ble_windows(force=False) is False


def test_ble_address_int_parses_macs_and_rejects_others() -> None:
    """A colon/dash MAC becomes a 48-bit int; a non-MAC (e.g. a macOS UUID) yields None."""
    parse = connection.MeshCoreDevice._ble_address_int
    assert parse(_BONDED_ADDR) == 0x001122334455
    assert parse("da-c5-21-6b-7c-7c") == 0xDAC5216B7C7C
    assert parse("not-a-mac") is None
    assert parse("550e8400-e29b-41d4-a716-446655440000") is None  # CoreBluetooth UUID


async def test_ble_pairing_helpers_noop_for_non_mac_address() -> None:
    """The bond query and unpair short-circuit (never touching WinRT) for a non-MAC address."""
    # A non-MAC address fails the parse before any winrt import, so these stay hermetic on any
    # platform — no real Bluetooth stack is consulted.
    assert await connection.MeshCoreDevice.is_ble_paired("not-a-mac") is False
    assert await connection.MeshCoreDevice.unpair_ble("not-a-mac") is False
    assert await connection.MeshCoreDevice.is_ble_paired("") is False


async def test_can_unpair_only_for_bonded_ble(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quit dialog offers unpair only on a BLE link that Windows actually holds a bond for."""

    async def _is_paired(address: str) -> bool:
        return address == _BONDED_ADDR

    monkeypatch.setattr(connection.MeshCoreDevice, "is_ble_paired", staticmethod(_is_paired))
    # Serial: never offered, whatever the address.
    serial_ctx = SimpleNamespace(active_transport="serial", active_address=None)
    assert await menu._can_unpair(serial_ctx) is False
    # BLE but no address to act on: not offered.
    ble_no_addr = SimpleNamespace(active_transport="ble", active_address=None)
    assert await menu._can_unpair(ble_no_addr) is False
    # BLE with a live bond: offered.
    bonded = SimpleNamespace(active_transport="ble", active_address=_BONDED_ADDR)
    assert await menu._can_unpair(bonded) is True
    # BLE but open (no bond, e.g. the PIN-less companion): not offered.
    open_ble = SimpleNamespace(active_transport="ble", active_address=_OPEN_ADDR)
    assert await menu._can_unpair(open_ble) is False


async def test_unpair_on_exit_disconnects_before_unpairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown drops the live link first, then forgets the OS bond (order matters)."""
    order: list[str] = []

    class _Dev:
        async def disconnect(self) -> None:
            order.append("disconnect")

    async def _unpair(address: str) -> bool:
        order.append(f"unpair:{address}")
        return True

    monkeypatch.setattr(connection.MeshCoreDevice, "unpair_ble", staticmethod(_unpair))
    ctx = SimpleNamespace(active_address=_BONDED_ADDR, _device=_Dev())
    await menu._unpair_on_exit(ctx)
    # Disconnect precedes unpair (a bond can't be dropped while in use), and the device handle
    # is released. The device_store is never touched — the remembered record survives.
    assert order == ["disconnect", f"unpair:{_BONDED_ADDR}"]
    assert ctx._device is None


def _make_ctx(tmp_path: Path) -> AppContext:
    """Build a real, mock-backed application context for reconnect tests."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "disc.db")
    return AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )


async def test_reconnect_rebuilds_device_and_restores_services(tmp_path: Path) -> None:
    """Reconnect opens a fresh connection and resumes the hub, monitor, and chat."""
    ctx = _make_ctx(tmp_path)
    try:
        await ctx.events.start()
        await ctx.monitor.start()  # start recording on the already-running hub
        await ctx.chat.start()
        original = await ctx.device()
        assert ctx.events.active and ctx.monitor.active and ctx.chat.active

        await ctx.reconnect()

        # A brand-new connection replaced the dead one, and everything that was running
        # before the drop is running again.
        rebuilt = await ctx.device()
        assert rebuilt is not original
        assert ctx.events.active
        assert ctx.monitor.active
        assert ctx.chat.active
    finally:
        await ctx.aclose()


async def test_ble_profile_opens_bluetooth_transport(tmp_path: Path, monkeypatch) -> None:
    """A BLE profile makes ``device()`` build a Bluetooth connection by address."""
    from meshterm.core import connection as conn
    from meshterm.core.config import DeviceProfile

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "ble.db")
    ctx = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        profile=DeviceProfile(name="handheld", transport="ble", address="AA:BB:CC:DD:EE:FF"),
    )

    built: dict = {}

    class _FakeBle:
        transport = "ble"

        def __init__(self, **kw):
            built.update(kw)
            self._port = None
            self._address = kw.get("address")

        async def connect(self):
            pass

        async def get_self_info(self):
            return {"name": "Handheld"}

    def fake_make_device(**kw):
        assert kw["transport"] == "ble"
        return _FakeBle(**kw)

    monkeypatch.setattr(conn, "make_device", fake_make_device)
    # context imported make_device by name, so patch the reference it actually calls.
    import meshterm.context as context_mod

    monkeypatch.setattr(context_mod, "make_device", fake_make_device)
    try:
        device = await ctx.device()
        assert device.transport == "ble"
        assert built["address"] == "AA:BB:CC:DD:EE:FF"
        assert ctx.active_transport == "ble"
        assert ctx.active_port is None  # BLE has no serial port to watch
    finally:
        ctx.repo.close()


async def test_tcp_profile_opens_network_transport(tmp_path: Path, monkeypatch) -> None:
    """A TCP profile makes ``device()`` build a network connection by host:port."""
    from meshterm.core import connection as conn
    from meshterm.core.config import DeviceProfile

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "tcp.db")
    ctx = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        profile=DeviceProfile(name="wifi", transport="tcp", host="192.168.1.50", tcp_port=5000),
    )

    built: dict = {}

    class _FakeTcp:
        transport = "tcp"

        def __init__(self, **kw):
            built.update(kw)
            self._port = None
            self._address = None
            self.endpoint = f"{kw.get('host')}:{kw.get('tcp_port')}"

        async def connect(self):
            pass

        async def get_self_info(self):
            return {"name": "WifiNode"}

    def fake_make_device(**kw):
        assert kw["transport"] == "tcp"
        return _FakeTcp(**kw)

    monkeypatch.setattr(conn, "make_device", fake_make_device)
    import meshterm.context as context_mod

    monkeypatch.setattr(context_mod, "make_device", fake_make_device)
    try:
        device = await ctx.device()
        assert device.transport == "tcp"
        assert built["host"] == "192.168.1.50" and built["tcp_port"] == 5000
        assert ctx.active_transport == "tcp"
        assert ctx.active_endpoint == "192.168.1.50:5000"
        assert ctx.active_port is None and ctx.active_address is None
    finally:
        ctx.repo.close()


async def test_reconnect_leaves_idle_services_idle(tmp_path: Path) -> None:
    """Reconnect only restores what was live: idle services stay idle afterward."""
    ctx = _make_ctx(tmp_path)
    try:
        original = await ctx.device()  # a tool opened the radio lazily; nothing subscribed

        await ctx.reconnect()

        rebuilt = await ctx.device()
        assert rebuilt is not original
        assert not ctx.events.active
        assert not ctx.monitor.active
        assert not ctx.chat.active
    finally:
        await ctx.aclose()


async def test_reconnect_restores_services_after_failed_attempts(
    tmp_path: Path, monkeypatch
) -> None:
    """Resume intent survives retries: services restore even if early reconnects fail.

    Reproduces the real-hardware bug where the first (failed) reconnect attempt tore the
    services down, so later attempts read the now-idle flags and restored nothing. The
    mock never fails ``device()``, so we force the first two ``device()`` calls to raise —
    as an absent port does — before letting the third (and the restore calls) succeed.
    """
    ctx = _make_ctx(tmp_path)
    try:
        await ctx.events.start()
        await ctx.monitor.start()
        await ctx.chat.start()
        await ctx.device()
        assert ctx.events.active and ctx.monitor.active and ctx.chat.active

        real_device = AppContext.device
        attempts = {"n": 0}

        async def flaky_device(self: AppContext):
            attempts["n"] += 1
            if attempts["n"] <= 2:  # the device isn't back yet on the first two tries
                raise _SerialException("could not open port 'COM_TEST'")
            return await real_device(self)

        monkeypatch.setattr(AppContext, "device", flaky_device)

        # Two failed reconnects (device still absent), then a success — like a slow replug.
        for _ in range(2):
            try:
                await ctx.reconnect()
            except _SerialException:
                pass
        await ctx.reconnect()  # the third device() call succeeds; services restore

        assert ctx.events.active
        assert ctx.monitor.active
        assert ctx.chat.active
    finally:
        monkeypatch.undo()
        await ctx.aclose()


def test_serial_port_present_reflects_os_enumeration(monkeypatch) -> None:
    """A port is 'present' iff it appears in the OS enumeration; the primary unplug signal."""
    pytest.importorskip("serial")
    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", lambda: [SimpleNamespace(device="COM11")])
    assert connection.serial_port_present("COM11")
    assert not connection.serial_port_present("COM99")


def test_serial_port_present_assumes_up_on_enumeration_error(monkeypatch) -> None:
    """A failed port query must never fake a disconnect — it reports 'present'."""
    pytest.importorskip("serial")
    from serial.tools import list_ports

    def boom():
        raise OSError("enumeration failed")

    monkeypatch.setattr(list_ports, "comports", boom)
    assert connection.serial_port_present("COM11")


def test_serial_port_present_platform_uart_by_path(monkeypatch) -> None:
    """A platform UART counts as present by its ``/dev`` node, not by a scan.

    A soldered UART (``/dev/ttyS1`` on the Luckfox Lyra) is invisible to pyserial's
    ``comports()``, but its char-device node persists — so an existing character
    device reads as present, while a vanished node (a real USB unplug of
    ``/dev/ttyUSB*``) still reads as absent.
    """
    pytest.importorskip("serial")
    import os as _os
    import stat as _stat

    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", list)  # platform UARTs aren't enumerated -> []
    monkeypatch.setattr(_os.path, "exists", lambda p: p == "/dev/ttyS1")
    monkeypatch.setattr(_os, "stat", lambda p: SimpleNamespace(st_mode=_stat.S_IFCHR))

    assert connection.serial_port_present("/dev/ttyS1")  # existing char device -> present
    assert not connection.serial_port_present("/dev/ttyUSB9")  # node gone -> absent (unplug)


async def test_wait_for_disconnect_fires_when_port_vanishes(tmp_path: Path, monkeypatch) -> None:
    """The liveness watcher resolves once the connected device's port leaves enumeration."""
    ctx = _make_ctx(tmp_path)
    # Pose as a live real-hardware session on COM_TEST (the mock can't be unplugged).
    ctx.mock = False
    ctx._device = _FakeSerialDevice("COM_TEST")  # connected device; is_connected -> True
    ctx._active_transport = "serial"
    ctx._active_port = "COM_TEST"

    checks = {"n": 0}

    def fake_present(port: str) -> bool:
        checks["n"] += 1
        return checks["n"] < 2  # present on the first poll, gone thereafter

    monkeypatch.setattr(connection, "serial_port_present", fake_present)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "_LIVENESS_CONFIRM_S", 0.0)
    try:
        await asyncio.wait_for(menu._wait_for_disconnect(ctx), timeout=2.0)
    finally:
        ctx.repo.close()


async def test_wait_for_disconnect_ignores_the_simulator(tmp_path: Path) -> None:
    """The watcher never fires for --mock: the simulator has no port to lose."""
    ctx = _make_ctx(tmp_path)  # mock=True
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(menu._wait_for_disconnect(ctx), timeout=0.2)
    finally:
        ctx.repo.close()


class _FakeBleDevice:
    """A minimal stand-in for a connected BLE :class:`Device` in liveness tests.

    Its :meth:`link_present` reads an ``is_connected`` flag, mirroring how the real BLE
    device reads the meshcore client's connection state.
    """

    transport = "ble"

    def __init__(self) -> None:
        self.is_connected = True

    async def link_present(self) -> bool:
        return self.is_connected


async def test_wait_for_disconnect_fires_when_ble_link_drops(tmp_path: Path, monkeypatch) -> None:
    """The same watcher fires for BLE once the peripheral's connection flag flips false."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    device = _FakeBleDevice()
    ctx._device = device
    ctx._active_transport = "ble"
    ctx._active_address = "AA:BB:CC:DD:EE:FF"

    async def drop_soon() -> None:
        device.is_connected = False  # the peripheral goes out of range

    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "_LIVENESS_CONFIRM_S", 0.0)
    try:
        await drop_soon()
        await asyncio.wait_for(menu._wait_for_disconnect(ctx), timeout=2.0)
    finally:
        ctx.repo.close()


class _FakeSession:
    """A minimal stand-in for :class:`TuiSession` recording pushes/pops for dialog tests."""

    def __init__(self) -> None:
        self.stack: list = []
        self.root = None  # no menu ever declared itself: the dialog gets a blank base

    def push(self, screen) -> None:
        self.stack.append(screen)

    def pop(self, screen=None) -> None:
        if self.stack:
            self.stack.pop()

    def invalidate(self) -> None:
        pass


async def test_handle_disconnect_auto_reconnects_when_port_returns(
    tmp_path: Path, monkeypatch
) -> None:
    """The popup dismisses itself (no keypress) once the device's port re-appears."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = object()  # a connected stand-in
    ctx._active_port = "COM_TEST"

    polls = {"n": 0}

    def fake_present(port: str) -> bool:
        polls["n"] += 1
        return polls["n"] >= 2  # absent on the first poll, back thereafter

    async def fake_reconnect(self: AppContext, *, ble_device: object | None = None) -> None:
        self._device = object()  # a fresh connection

    monkeypatch.setattr(connection, "serial_port_present", fake_present)
    monkeypatch.setattr(AppContext, "reconnect", fake_reconnect)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "spinner_interval", lambda: 0.0)

    session = _FakeSession()
    try:
        quit_chosen = await asyncio.wait_for(menu._handle_disconnect(ctx, session), timeout=2.0)
        assert quit_chosen is False  # reconnected, not quit
        assert session.stack == []  # the popup was cleaned up
    finally:
        ctx.repo.close()


async def test_handle_disconnect_quits_when_user_presses_quit(tmp_path: Path, monkeypatch) -> None:
    """Pressing Enter (the Abort button) leaves, even while the device is still gone."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = object()
    ctx._active_port = "COM_TEST"

    # The port never comes back, so the only way out is the Quit button.
    monkeypatch.setattr(connection, "serial_port_present", lambda port: False)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "spinner_interval", lambda: 0.0)

    session = _FakeSession()

    async def press_quit() -> None:
        # Once the dialog is on the stack, deliver Enter to its Abort button.
        while not session.stack:
            await asyncio.sleep(0)
        session.stack[-1].handle("enter")

    try:
        _, quit_chosen = await asyncio.wait_for(
            asyncio.gather(press_quit(), menu._handle_disconnect(ctx, session)),
            timeout=2.0,
        )
        assert quit_chosen is True
        assert session.stack == []
    finally:
        ctx.repo.close()


async def test_reconnect_dialog_aborts_on_enter_and_ignores_escape() -> None:
    """Enter aborts (resolves ``"quit"``); Esc is inert so a stray keypress can't drop us."""
    from meshterm.ui.tui import ReconnectDialog

    dialog = ReconnectDialog("Waiting…")
    dialog.future = asyncio.get_running_loop().create_future()

    dialog.handle("escape")
    assert not dialog.future.done()  # Esc does nothing

    dialog.handle("enter")
    assert dialog.future.result() == "quit"


async def _never() -> None:
    """An awaitable that blocks forever (a stand-in for an idle worker/watcher)."""
    await asyncio.Event().wait()


async def test_session_loop_quits_without_touching_the_disconnect_path(monkeypatch) -> None:
    """When the menu loop returns (user quit), the watcher is stopped and no dialog shows."""
    handled = {"n": 0}

    async def fake_menu(ctx, session):
        return None  # user quit at the menu

    async def fake_handle(ctx, session):
        handled["n"] += 1
        return True

    monkeypatch.setattr(menu, "_menu_loop", fake_menu)
    monkeypatch.setattr(menu, "_wait_for_disconnect", lambda ctx: _never())
    monkeypatch.setattr(menu, "_handle_disconnect", fake_handle)

    session = SimpleNamespace(reset=lambda: None)
    await asyncio.wait_for(menu._session_loop(object(), session), timeout=2)
    assert handled["n"] == 0  # the disconnect path was never entered


async def test_session_loop_cancels_worker_and_prompts_on_disconnect(monkeypatch) -> None:
    """A disconnect while the menu is busy cancels the worker, clears the stack, and prompts."""
    resets = {"n": 0}
    cancelled = {"seen": False}

    async def busy_menu(ctx, session):
        try:
            await asyncio.Event().wait()  # a tool is mid-flight; it must be cancelled
        except asyncio.CancelledError:
            cancelled["seen"] = True
            raise

    async def fires_now(ctx):
        return None  # the port vanished

    async def quit_at_dialog(ctx, session):
        return True

    monkeypatch.setattr(menu, "_menu_loop", busy_menu)
    monkeypatch.setattr(menu, "_wait_for_disconnect", fires_now)
    monkeypatch.setattr(menu, "_handle_disconnect", quit_at_dialog)

    session = SimpleNamespace(reset=lambda: resets.__setitem__("n", resets["n"] + 1))
    await asyncio.wait_for(menu._session_loop(object(), session), timeout=2)
    assert cancelled["seen"]  # the in-flight worker was cancelled
    assert resets["n"] == 1  # the stack was unwound before the dialog


async def test_session_loop_resumes_a_fresh_menu_after_reconnect(monkeypatch) -> None:
    """After a reconnect the loop starts a new menu (and re-arms the watcher)."""
    state = {"menu": 0, "watch": 0, "handle": 0}

    async def flaky_menu(ctx, session):
        state["menu"] += 1
        if state["menu"] == 1:
            await asyncio.Event().wait()  # first pass: interrupted by the disconnect
        return None  # second pass: user quits

    async def watch(ctx):
        state["watch"] += 1
        if state["watch"] == 1:
            return None  # fire once
        await asyncio.Event().wait()  # never fire again

    async def reconnected(ctx, session):
        state["handle"] += 1
        return False  # the device came back

    monkeypatch.setattr(menu, "_menu_loop", flaky_menu)
    monkeypatch.setattr(menu, "_wait_for_disconnect", watch)
    monkeypatch.setattr(menu, "_handle_disconnect", reconnected)

    session = SimpleNamespace(reset=lambda: None)
    await asyncio.wait_for(menu._session_loop(object(), session), timeout=2)
    assert state["handle"] == 1  # one disconnect handled
    assert state["menu"] == 2  # a fresh menu ran after reconnect


class _WedgedMeshCore:
    """A fake meshcore client whose graceful ``disconnect()`` never returns.

    Reproduces the library's dispatcher-stop deadlock (``queue.join()`` with events still
    queued after the processor task exited), which used to hang MeshTerm's exit until the
    watchdog force-killed the process. Records whether the forced path ran.
    """

    def __init__(self) -> None:
        self.force_stopped = False
        self.connection_manager = SimpleNamespace(connection=self)
        self.raw_closed = False

    async def disconnect(self) -> None:
        # Called both as the graceful teardown (via the manager-less attribute lookup on
        # the client) and as the raw transport close. The graceful call wedges; the raw
        # close is distinguished by the force-stop having run first.
        if not self.force_stopped:
            await asyncio.Event().wait()  # the dispatcher deadlock: never returns
        self.raw_closed = True

    def stop(self) -> None:
        self.force_stopped = True


async def test_disconnect_bounds_a_wedged_client_teardown(monkeypatch) -> None:
    """A deadlocked graceful teardown is abandoned and the transport force-closed instead."""
    monkeypatch.setattr(connection, "_DISCONNECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(connection, "_FORCE_DISCONNECT_TIMEOUT_S", 0.5)

    dev = connection.MeshCoreDevice(port="COM_TEST")
    wedged = _WedgedMeshCore()
    dev._mc = wedged

    await asyncio.wait_for(dev.disconnect(), timeout=2.0)  # must not hang

    assert wedged.force_stopped  # the dispatcher task was cancelled synchronously
    assert wedged.raw_closed  # the port/link was still released
    assert dev._mc is None  # idempotent: a second disconnect is a no-op


async def test_disconnect_graceful_path_needs_no_force(monkeypatch) -> None:
    """A healthy teardown completes gracefully; the forced path is never entered."""

    class _HealthyMeshCore:
        def __init__(self) -> None:
            self.disconnected = False
            self.force_stopped = False
            self.connection_manager = SimpleNamespace(connection=self)

        async def disconnect(self) -> None:
            self.disconnected = True

        def stop(self) -> None:
            self.force_stopped = True

    dev = connection.MeshCoreDevice(port="COM_TEST")
    healthy = _HealthyMeshCore()
    dev._mc = healthy

    await dev.disconnect()

    assert healthy.disconnected
    assert not healthy.force_stopped
    assert dev._mc is None


class _FakeBleDevice:
    """A stand-in for a connected BLE :class:`Device` whose link has already dropped."""

    transport = "ble"

    def __init__(self) -> None:
        self.disconnected = False

    async def link_present(self) -> bool:
        return False

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected = True

    async def get_self_info(self) -> dict:
        return {}

    async def get_device_info(self) -> dict:
        return {}


def _ble_ctx(tmp_path: Path) -> AppContext:
    """A context standing in for a live BLE session whose companion has just dropped."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = _FakeBleDevice()
    ctx._active_transport = "ble"
    ctx._active_address = "AA:BB:00:00:00:01"
    return ctx


async def test_ble_reconnect_waits_for_the_advertisement_and_hands_over_the_fresh_handle(
    tmp_path: Path, monkeypatch
) -> None:
    """BLE reconnects on hearing the companion, opening it with the handle just scanned."""
    ctx = _ble_ctx(tmp_path)
    fresh = object()  # the live BLEDevice the scan produced
    scans: list[str] = []

    async def fake_find(address: str, timeout: float = 0.0) -> object | None:
        scans.append(address)
        return fresh if len(scans) >= 2 else None  # silent on the first round, then heard

    opened: list[object | None] = []

    async def fake_reconnect(self: AppContext, *, ble_device: object | None = None) -> None:
        opened.append(ble_device)
        self._device = _FakeBleDevice()

    monkeypatch.setattr(discovery, "find_ble_device", fake_find)
    monkeypatch.setattr(AppContext, "reconnect", fake_reconnect)
    monkeypatch.setattr(menu, "_BLE_RESCAN_PAUSE_S", 0.0)
    monkeypatch.setattr(menu, "spinner_interval", lambda: 0.0)

    session = _FakeSession()
    try:
        quit_chosen = await asyncio.wait_for(menu._handle_disconnect(ctx, session), timeout=2.0)
        assert quit_chosen is False  # reconnected, not quit
        # It waited for the device to be heard rather than blind-retrying the connect...
        assert scans == [ctx._active_address, ctx._active_address]
        assert len(opened) == 1
        # ...and reopened it with the handle that scan produced, not a stale one.
        assert opened == [fresh]
    finally:
        ctx.repo.close()


async def test_ble_reconnect_releases_the_dead_link_before_scanning(
    tmp_path: Path, monkeypatch
) -> None:
    """The old link is put down first, so the peripheral is free to advertise again."""
    ctx = _ble_ctx(tmp_path)
    dead = ctx._device
    held_at_scan: list[bool] = []

    async def fake_find(address: str, timeout: float = 0.0) -> object | None:
        held_at_scan.append(ctx._device is not None)
        return object()

    async def fake_reconnect(self: AppContext, *, ble_device: object | None = None) -> None:
        self._device = _FakeBleDevice()

    monkeypatch.setattr(discovery, "find_ble_device", fake_find)
    monkeypatch.setattr(AppContext, "reconnect", fake_reconnect)
    monkeypatch.setattr(menu, "spinner_interval", lambda: 0.0)

    session = _FakeSession()
    try:
        await asyncio.wait_for(menu._handle_disconnect(ctx, session), timeout=2.0)
        assert dead.disconnected  # the dead companion was let go...
        assert held_at_scan == [False]  # ...before we ever listened for it
    finally:
        ctx.repo.close()


async def test_reconnect_prefers_the_freshly_scanned_ble_handle(
    tmp_path: Path, monkeypatch
) -> None:
    """A handle passed to reconnect opens the link; the startup scan's stale one is not reused."""
    address = "AA:BB:00:00:00:01"
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx.ble_override = address
    stale = object()  # what the picker's scan produced when the session opened
    fresh = object()  # what the reconnect flow just heard advertising
    ctx.selected_device = DiscoveredDevice(
        transport="ble", address=address, name="MeshCore-Homestead", ble_device=stale
    )
    handles: list[object | None] = []

    def fake_make_device(**kwargs: object):  # noqa: ANN202 - a stub stands in for the radio
        handles.append(kwargs.get("ble_device"))
        return _FakeBleDevice()

    monkeypatch.setattr(context_module, "make_device", fake_make_device)
    try:
        await ctx.reconnect(ble_device=fresh)
        assert handles == [fresh]  # the freshly-scanned peripheral, never the stale handle
        assert ctx._ble_handle is None  # consumed by the connection it opened

        # With nothing scanned, the picker's handle is still the best available guess.
        await ctx.reconnect()
        assert handles == [fresh, stale]
    finally:
        ctx.repo.close()


async def test_release_link_is_idempotent_and_holds_the_resume_intent(tmp_path: Path) -> None:
    """Releasing twice is harmless, and what was running is still restored on reconnect."""
    ctx = _make_ctx(tmp_path)
    try:
        await ctx.events.start()
        await ctx.monitor.start()
        await ctx.release_link()
        assert not ctx.monitor.active  # the services rode the link down with it
        await ctx.release_link()  # a second release finds nothing to do and says nothing

        await ctx.reconnect()

        assert ctx.events.active and ctx.monitor.active  # the held intent survived both
    finally:
        await ctx.aclose()


class _AbandonedBleakClient:
    """A ``bleak`` client whose peripheral vanished: already down, and never handed back.

    Mirrors what the meshcore transport leaves behind on a dropped link — ``is_connected`` is
    already ``False``, so every guarded teardown in the library declines to touch it.
    """

    def __init__(self) -> None:
        self.is_connected = False
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class _DroppedBleConnection:
    """meshcore's ``BLEConnection`` after its dropped-link callback ran: client reference gone."""

    def __init__(self) -> None:
        self.client = None  # handle_disconnect restored this to the constructor's value
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1  # guards on self.client — reaches nothing


class _DroppedMeshCore:
    """A meshcore client whose connection manager also believes it is already disconnected."""

    def __init__(self, connection: _DroppedBleConnection) -> None:
        self.connection_manager = SimpleNamespace(connection=connection)
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True  # guards on _is_connected — reaches nothing

    def stop(self) -> None:
        pass


async def test_a_vanished_peripheral_still_gets_its_bleak_client_closed() -> None:
    """The client is released even though every library guard says there is nothing to close."""
    dev = connection.MeshCoreDevice(transport="ble", address="AA:BB:00:00:00:01")
    dropped = _DroppedBleConnection()
    dev._mc = _DroppedMeshCore(dropped)
    abandoned = _AbandonedBleakClient()
    dev._ble_client = abandoned  # what we kept hold of at connect

    await dev.disconnect()

    # The library's own teardown reached nothing — the connection had already let the client go.
    assert dropped.client is None
    # Ours closed it anyway, which is what frees the WinRT handles the next connect needs.
    assert abandoned.disconnect_calls == 1
    assert dev._ble_client is None  # and it is not closed twice
    assert dev._mc is None


async def test_releasing_the_bleak_client_is_idempotent_and_never_raises() -> None:
    """A second disconnect finds nothing to do, and a failing close is swallowed."""
    dev = connection.MeshCoreDevice(transport="ble", address="AA:BB:00:00:00:01")

    await dev.disconnect()  # nothing connected at all
    assert dev._ble_client is None

    class _RefusesToClose:
        is_connected = False

        async def disconnect(self) -> None:
            raise OSError("the handle is invalid")

    dev._ble_client = _RefusesToClose()
    await dev.disconnect()  # must not raise: teardown is best-effort
    assert dev._ble_client is None


async def test_a_healthy_bluetooth_teardown_still_releases_the_client() -> None:
    """The graceful path runs as before, and the client is released after it."""
    dev = connection.MeshCoreDevice(transport="ble", address="AA:BB:00:00:00:01")

    class _HealthyMeshCore:
        def __init__(self) -> None:
            self.disconnected = False
            self.force_stopped = False
            self.connection_manager = SimpleNamespace(connection=self)

        async def disconnect(self) -> None:
            self.disconnected = True

        def stop(self) -> None:
            self.force_stopped = True

    healthy = _HealthyMeshCore()
    dev._mc = healthy
    live = _AbandonedBleakClient()
    live.is_connected = True
    dev._ble_client = live

    await dev.disconnect()

    assert healthy.disconnected
    assert not healthy.force_stopped  # the graceful path was enough, as it always was
    assert live.disconnect_calls == 1  # and the client is released regardless
