# SPDX-License-Identifier: Apache-2.0
"""Tests for the graceful handling of a lost device connection.

The tests cover two things. The first is the exception classifier, which finds the
difference between a dropped serial link and an ordinary command failure. The second is
:meth:`AppContext.reconnect`. It builds the connection again and restores the services
(event hub, passive monitor, chat) that ran before the drop. All the tests run against the
:class:`MockDevice` simulator, so no hardware is necessary.
"""

from __future__ import annotations

import asyncio
import errno
import io
import socket
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
    """A stand-in for ``serial.SerialException``.

    The classifier matches the type name, not the import.
    """


class _FakeSerialDevice:
    """A minimal stand-in for a connected serial :class:`Device` in the liveness tests.

    Its :meth:`link_present` is the same as in the real serial device. It reports if the
    operating system still lists the port. It reads through the module function that the
    tests patch, so the tests can switch the presence on and off.
    """

    transport = "serial"

    def __init__(self, port: str = "COM_TEST") -> None:
        self._port = port

    async def link_present(self) -> bool:
        return connection.serial_port_present(self._port)


def test_is_connection_lost_matches_serial_exception() -> None:
    """The classifier finds a pyserial read or write failure to be a dropped link."""
    assert is_connection_lost(_SerialException("ClearCommError failed (Access is denied.)"))


def test_is_connection_lost_walks_the_exception_chain() -> None:
    """A link error in a higher-level error is still found through its cause."""
    try:
        try:
            raise _SerialException("WriteFile failed")
        except Exception as cause:
            raise RuntimeError("device write failed") from cause
    except Exception as exc:
        assert is_connection_lost(exc)


def test_is_connection_lost_matches_os_level_drops() -> None:
    """A teardown by the operating system, or an I/O message that shows a drop, is a lost link."""
    assert is_connection_lost(ConnectionResetError("reset"))
    assert is_connection_lost(OSError("input/output error"))


def test_is_connection_lost_ignores_ordinary_failures() -> None:
    """A short command timeout or a generic error is not a dropped link."""
    assert not is_connection_lost(DeviceCommandError("the companion didn't respond in time"))
    assert not is_connection_lost(ValueError("bad value"))


# These stand-ins have the names of the real bleak classes, because the classifier matches
# the type name (not the import). Refer to ``_CONNECTION_LOST_TYPES``.
class BleakError(Exception):  # noqa: N818 - the same name as bleak's own, with no Error suffix
    """A stand-in for ``bleak.exc.BleakError``.

    The classifier matches the type name, not the import.
    """


class BleakDeviceNotFoundError(Exception):
    """A stand-in for ``bleak.exc.BleakDeviceNotFoundError``."""


def test_is_connection_lost_matches_ble_errors() -> None:
    """A dropped Bluetooth link is a lost connection, the same as a serial unplug."""
    assert is_connection_lost(BleakError("gatt operation failed"))  # Matched by type name.
    assert is_connection_lost(BleakDeviceNotFoundError("AA:BB:CC not found"))
    # The meshcore BLE transport reports a lost link with a reason string in a callback.
    assert is_connection_lost(RuntimeError("ble_transport_lost"))


class BleakGATTProtocolError(Exception):
    """A stand-in for the GATT authentication rejection of bleak.

    The classifier matches its message, not the import.
    """


def test_ble_auth_error_is_recognized_and_is_not_a_lost_link() -> None:
    """A PIN or pairing rejection is a separate case. It is not a dropped link.

    The user can act on this case, so the classifier must not confuse it with a drop.
    """
    exc = BleakGATTProtocolError("(5, 'GATT Protocol Error: Insufficient Authentication')")
    assert connection._is_ble_auth_error(exc)
    assert not is_connection_lost(exc)  # Thus the session offers a PIN, not a reconnect.


def test_ble_auth_error_walks_the_exception_chain() -> None:
    """An authentication rejection in a meshcore transport error is found through its cause."""
    try:
        try:
            raise BleakGATTProtocolError("Insufficient Encryption")
        except Exception as cause:
            raise RuntimeError("connect failed") from cause
    except Exception as exc:
        assert connection._is_ble_auth_error(exc)


#: Stand-in Bluetooth addresses. They are never the address of a real companion. A test
#: that names a real address puts a personal device into the repository. Also, a link to
#: hardware here is false, because no test in this file touches a radio.
_BONDED_ADDR = "00:11:22:33:44:55"
_OPEN_ADDR = "AA:BB:CC:DD:EE:FF"
#: A stand-in pairing code, for the same reason. The tests check only that a code was supplied.
_A_PIN = "123456"


@pytest.fixture(autouse=True)
def _no_bluez_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the BlueZ agent of the connect step off the system bus when these tests run on Linux."""
    from contextlib import nullcontext

    monkeypatch.setattr(connection.MeshCoreDevice, "_ble_pairing_agent", lambda self: nullcontext())


def test_an_unlikely_error_on_the_subscribe_is_a_pairing_refusal() -> None:
    """ATT 0x0E is how a companion that has a PIN refuses a subscribe over an unauthenticated bond.

    This happened on a uConsole. Its bond with a T1000-E was older than the PIN of the
    T1000-E (BlueZ Authenticated=0). The encryption succeeded, but the subscribe did not.
    The user saw only the raw GATT error.
    """
    exc = BleakGATTProtocolError(
        "(<BleakGATTProtocolErrorCode.UNLIKELY_ERROR: 14>, 'GATT Protocol Error: Unlikely Error')"
    )
    assert connection._is_ble_auth_error(exc)


class _RetryingBleakClient:
    """A bleak client whose connect step drops its first link attempts and then succeeds."""

    def __init__(self, drops: int, callback) -> None:
        self.drops, self.callback = drops, callback
        self.is_connected = False
        self.disconnects = 0

    async def connect(self) -> None:
        for _ in range(self.drops):
            self.callback(self)  # bleak calls the disconnect callback for each lost attempt.
        self.is_connected = True

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.is_connected = False


class _MeshcoreLikeConnection:
    """The same as the BLEConnection of meshcore.

    A disconnect sets ``client`` to the value that was passed.
    """

    def __init__(self, drops: int) -> None:
        self.client = None
        self.drops = drops
        self.forwarded = 0

    def handle_disconnect(self, client) -> None:
        self.forwarded += 1
        self.client = None  # This reset left the live link with no owner.

    async def connect(self):
        self.client = _RetryingBleakClient(self.drops, self.handle_disconnect)
        await self.client.connect()
        # meshcore calls start_notify on None, and reports "not established".
        if self.client is None:
            return None
        return "address"


async def test_a_retried_link_attempt_no_longer_strands_the_client() -> None:
    """The retries in bleak must not make meshcore drop the client that connects after them.

    This failure happened on the uConsole, in three runs out of three. Each lost link
    attempt called the disconnect handler of meshcore, and the handler set ``client`` to
    ``None``. The next retry of bleak connected a client that nobody held. Then meshcore
    gave up. The live link stopped the companion from advertising to the retry.
    """
    unguarded = _MeshcoreLikeConnection(drops=3)
    assert await unguarded.connect() is None  # This is the bug, reproduced.

    guarded = _MeshcoreLikeConnection(drops=3)
    seen = connection._hold_disconnects_while_connecting(guarded)
    assert await guarded.connect() == "address"
    seen.connecting = False
    assert guarded.forwarded == 0  # The disconnects are held while the connect step runs.
    assert connection._held_client(guarded, seen) is guarded.client
    guarded.client.callback(guarded.client)  # A real drop after that still arrives.
    assert guarded.forwarded == 1


def test_the_held_client_falls_back_to_the_one_meshcore_lost() -> None:
    """If meshcore lets go of the client, the teardown still gets the live client to close."""
    lost = _RetryingBleakClient(0, None)
    lost.is_connected = True
    seen = connection._ConnectingClients([lost])
    assert connection._held_client(SimpleNamespace(client=None), seen) is lost


async def _disable_windows_pairing(dev: connection.MeshCoreDevice) -> None:
    """Replace the WinRT ProvidePin step with a stub, so ``_create_ble`` stays isolated in tests.

    On a real Windows or Linux host, ``_pair_ble`` reaches the Bluetooth stack of the
    operating system and the physical device. A no-op in its place gives the same path as a
    host that is not Windows, or that has no winrt. Thus the tests can examine the logic
    that translates an authentication error without hardware.
    """

    async def _never_pairs(*, force: bool) -> bool:
        return False

    async def _no_bond() -> bool:
        return False

    dev._pair_ble = _never_pairs  # type: ignore[method-assign]
    dev._ble_os_bonded = _no_bond  # type: ignore[method-assign]


def _fake_ble_stack(monkeypatch: pytest.MonkeyPatch, *outcomes):
    """A stand-in ``MeshCore`` class, with one scripted entry for each connect attempt.

    MeshTerm builds the meshcore client itself (:meth:`~MeshCoreDevice._connect_owned_ble`).
    It does not let ``create_ble`` build the client and then leave it with no owner. Thus
    the fake here is a class, and the test makes one instance for each attempt. Each
    instance records if it was closed. This is the important property: a connect that
    fails must never leave its link open. If it does, the peripheral believes that it has a
    peer, and it stops advertising.

    Args:
        monkeypatch: Used to replace the ``meshcore`` module, which MeshTerm imports late,
            with a stub. Thus no real ``BLEConnection`` (and no ``bleak``) is necessary.
        outcomes: What each next ``connect()`` does. An exception instance is raised. Any
            other value is returned (``None`` is a handshake that got no answer).

    Returns:
        The fake class. Its ``built`` list holds the instances that it made, in order.
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
    """A raw GATT authentication rejection becomes an error that names the PIN solution.

    The error is a DeviceAuthenticationError.
    """
    # The test sets the platform to not macOS. On macOS, the same rejection starts a pairing
    # that the operating system runs, and MeshTerm retries it and does not report it. There,
    # the advice is the system dialog and not --ble-pin. Without this line, the test checks
    # the Windows and Linux words on the macOS CI runner, and it fails.
    monkeypatch.setattr(sys, "platform", "win32")
    # No PIN was supplied, so the message tells the user to pass one. The subclass lets the
    # interactive picker catch "needs a PIN" alone, and the CLI still catches it as a
    # DeviceCommandError.
    fake = _fake_ble_stack(monkeypatch, BleakGATTProtocolError("Insufficient Authentication"))
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    await _disable_windows_pairing(dev)
    with pytest.raises(connection.DeviceAuthenticationError) as excinfo:
        await dev._create_ble(fake)
    assert isinstance(excinfo.value, DeviceCommandError)  # Thus the scripted CLI catches it too.
    assert "--ble-pin" in str(excinfo.value)
    # MeshTerm released the failed link and did not leave it open.
    assert fake.built[0].disconnect_calls == 1

    # A PIN was supplied but the device rejected it. The message says the PIN was wrong. It
    # does not say that no PIN was given.
    fake_pin = _fake_ble_stack(monkeypatch, BleakGATTProtocolError("Insufficient Authentication"))
    dev_pin = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR, pin=_A_PIN)
    await _disable_windows_pairing(dev_pin)
    with pytest.raises(connection.DeviceAuthenticationError) as excinfo_pin:
        await dev_pin._create_ble(fake_pin)
    assert "rejected" in str(excinfo_pin.value).lower()


async def test_create_ble_names_a_stale_windows_bond(monkeypatch) -> None:
    """There is no PIN, but Windows holds a bond that the device refuses.

    The message says that the bond is stale. A user reported this case. Windows showed the
    companion as paired, but the companion refused the subscribe, because the device side
    of the bond was lost. The message "Requires a PIN" was wrong for a user who saw
    "Paired" in Settings.
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
    assert "--ble-pin" in str(excinfo.value)  # The PIN is still how MeshTerm pairs it again.
    assert "saved pairing" in excinfo.value.hint  # Also, the PIN dialog says why it asks.


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
    """With a PIN, the refusal names the outcome of the pairing. Each outcome has a solution."""
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

    dev._pair_ble = _pair  # type: ignore[method-assign]
    with pytest.raises(connection.DeviceAuthenticationError) as excinfo:
        await dev._create_ble(fake)
    assert expected in str(excinfo.value)
    assert hint in excinfo.value.hint


def test_a_refused_pairing_step_is_an_auth_error() -> None:
    """A pair() call that fails in bleak is a pairing problem, not a device that did not answer."""
    assert connection._is_ble_auth_error(Exception("Could not pair with device: 19: FAILED"))
    assert connection._is_ble_auth_error(Exception("org.bluez.Error.AuthenticationFailed"))


@pytest.mark.parametrize(
    ("stage", "expected"), [("connect", "took longer"), ("identity", "identity")]
)
async def test_ble_probe_timeout_names_its_stage(monkeypatch, stage: str, expected: str) -> None:
    """A BLE probe that runs out of time says which stage stopped. It does not return None."""
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
    """A first authentication failure causes one unpair and pair again, then a connect that works.

    The test models the Windows upgrade case. An old unauthenticated "Just Works" bond
    makes the first connect fail, also with the correct PIN. Thus ``_pair_ble(force=True)``
    clears the bond, and the second connect succeeds.
    """
    fake = _fake_ble_stack(
        monkeypatch, BleakGATTProtocolError("Insufficient Authentication"), "handshake-ok"
    )
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR, pin=_A_PIN)
    repairs: list[bool] = []

    async def _pair(*, force: bool) -> bool:
        repairs.append(force)
        # The pass before the connect (force=False) does nothing. The repair (force=True) works.
        return force

    dev._pair_ble = _pair  # type: ignore[method-assign]
    result = await dev._create_ble(fake)
    assert result is fake.built[1]  # The caller gets the client of the retry.
    assert len(fake.built) == 2  # It failed one time and MeshTerm retried one time.
    # The attempt before the connect, then the pairing again that repairs the bond.
    assert repairs == [False, True]
    assert fake.built[0].disconnect_calls == 1  # Also, MeshTerm closed the failed client first.
    assert fake.built[1].disconnect_calls == 0  # MeshTerm returns the live client open.


async def test_create_ble_gives_up_after_one_repair(monkeypatch) -> None:
    """A wrong PIN that never bonds fails cleanly. It does not loop on the repair retry."""
    fake = _fake_ble_stack(
        monkeypatch,
        BleakGATTProtocolError("Insufficient Authentication"),
        BleakGATTProtocolError("Insufficient Authentication"),
    )
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR, pin=_A_PIN)
    calls: list[bool] = []

    async def _pair(*, force: bool) -> bool:
        calls.append(force)
        return force  # Also the repair "succeeds", so the test proves that the retry runs one time.

    dev._pair_ble = _pair  # type: ignore[method-assign]
    with pytest.raises(connection.DeviceAuthenticationError):
        await dev._create_ble(fake)
    # First force=False (before the connect), then force=True (the repair). The retry of the
    # repair passes allow_repair=False. Thus there is no third pairing attempt, also if the
    # connect keeps failing.
    assert calls == [False, True]
    assert all(client.disconnect_calls == 1 for client in fake.built)


async def test_create_ble_retries_a_transient_link_failure(monkeypatch) -> None:
    """A ConnectionError at the transport level is retried one time, with the scanned BLEDevice.

    The test models a common failure on Windows. The first attempt to open the link misses
    the peripheral, which advertises slowly, and the meshcore client raises a bare
    ``ConnectionError``. Users learned to select the device again, which is a manual
    retry. Thus the connect step retries by itself before it reports the failure.
    """
    scanned_device = object()  # The BLEDevice that the discovery scan produced.
    fake = _fake_ble_stack(
        monkeypatch, ConnectionError("Failed to connect to device"), "handshake-ok"
    )
    monkeypatch.setattr(connection, "_BLE_CONNECT_RETRY_DELAY_S", 0.0)
    dev = connection.MeshCoreDevice(transport="ble", address=_OPEN_ADDR, ble_device=scanned_device)
    await _disable_windows_pairing(dev)
    assert await dev._create_ble(fake) is fake.built[1]
    assert len(fake.built) == 2  # It failed one time and MeshTerm retried one time.
    # Each attempt connects through the BLEDevice that the scan found. It never uses a bare address.
    assert all(client.cx.device is scanned_device for client in fake.built)


async def test_create_ble_gives_up_after_the_retry(monkeypatch) -> None:
    """A link that never opens says so after the limited retries, not "not a companion"."""
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
    assert isinstance(excinfo.value.__cause__, ConnectionError)  # The original error is the cause.
    assert len(fake.built) == connection._BLE_CONNECT_ATTEMPTS
    assert all(client.disconnect_calls == 1 for client in fake.built)


async def test_create_ble_never_retries_an_auth_rejection(monkeypatch) -> None:
    """A PIN or bond rejection is translated on the first attempt. The retry never loops on it."""
    # The platform is not macOS. On macOS, a rejection is the start of a pairing that the
    # operating system runs, and MeshTerm waits for it.
    monkeypatch.setattr(sys, "platform", "win32")
    fake = _fake_ble_stack(monkeypatch, BleakGATTProtocolError("Insufficient Authentication"))
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    await _disable_windows_pairing(dev)
    with pytest.raises(connection.DeviceAuthenticationError):
        await dev._create_ble(fake)
    assert len(fake.built) == 1


async def test_create_ble_waits_for_macos_to_finish_pairing(monkeypatch) -> None:
    """On macOS the first authentication rejection starts the pairing, so the connect waits.

    The only way to make CoreBluetooth pair is to touch the authenticated characteristic of
    the companion. Thus the rejection and the Passkey dialog are one event. MeshTerm once
    gave up at this point. It reported an error for a pairing that was successful, and the
    device connected only when the user selected it a second time.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(connection, "_BLE_MACOS_PAIRING_DELAY_S", 0)
    # The companion refuses two times while the dialog is open. Then the bond is made and the
    # subscribe works.
    fake = _fake_ble_stack(
        monkeypatch,
        BleakGATTProtocolError("Insufficient Authentication"),
        BleakGATTProtocolError("Insufficient Authentication"),
        "handshake-ok",
    )
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    await _disable_windows_pairing(dev)

    result = await dev._create_ble(fake)
    assert result is fake.built[2]  # The caller gets the attempt that MeshTerm made after the bond.
    assert len(fake.built) == 3
    # MeshTerm released each refused link and did not leave it open.
    assert fake.built[0].disconnect_calls == 1
    assert fake.built[1].disconnect_calls == 1
    assert fake.built[2].disconnect_calls == 0  # MeshTerm returns the live client open.


async def test_create_ble_gives_up_when_macos_pairing_is_dismissed(monkeypatch) -> None:
    """A dialog that nobody answers fails in the end. The error says where the code is asked for."""
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
    assert len(fake.built) == connection._BLE_MACOS_PAIRING_ATTEMPTS + 1  # The number is limited.
    # --ble-pin cannot help on macOS, because the operating system collects the code. Thus
    # the message must not send the user to --ble-pin.
    assert "--ble-pin" not in str(excinfo.value)
    assert "System Settings" in str(excinfo.value)


async def test_connect_reports_an_unanswered_handshake_and_closes_it(monkeypatch) -> None:
    """The transport is up but no identity reply comes. The error is a clean "not a companion".

    MeshTerm also releases the link.
    """
    fake = _fake_ble_stack(monkeypatch, None)
    dev = connection.MeshCoreDevice(transport="ble", address=_OPEN_ADDR)
    await _disable_windows_pairing(dev)
    with pytest.raises(DeviceCommandError):
        await dev.connect()
    assert fake.built[0].disconnect_calls == 1


async def test_pair_ble_windows_noops_without_pin() -> None:
    """MeshTerm skips the pairing when no PIN is set, and it does not touch WinRT on this path."""
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    assert await dev._pair_ble_windows(force=False) is False


@pytest.mark.parametrize(
    ("platform", "ceremony"),
    [("win32", "_pair_ble_windows"), ("linux", "_pair_ble_bluez"), ("darwin", None)],
)
async def test_pair_ble_runs_this_platform_s_ceremony(monkeypatch, platform, ceremony) -> None:
    """Windows pairs through WinRT and Linux pairs through a BlueZ agent. macOS does not pair."""
    monkeypatch.setattr(sys, "platform", platform)
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR, pin=_A_PIN)
    ran: list[str] = []
    for name in ("_pair_ble_windows", "_pair_ble_bluez"):

        async def _ceremony(*, force: bool, name=name) -> bool:
            ran.append(name)
            return True

        setattr(dev, name, _ceremony)
    assert await dev._pair_ble(force=False) is (ceremony is not None)
    assert ran == ([ceremony] if ceremony else [])


async def test_linux_pairing_records_what_bluez_found(monkeypatch) -> None:
    """The BlueZ outcome goes into ``_ble_pairing``, and the refusal message reads it there."""
    from meshterm.core import bluez

    async def _pair(address, pin, *, force):
        return "failed", "authentication failed"

    monkeypatch.setattr(bluez, "pair", _pair)
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR, pin=_A_PIN)
    assert await dev._pair_ble_bluez(force=False) is False
    assert dev._ble_pairing == connection._BlePairing("failed", "authentication failed")


@pytest.mark.parametrize(
    ("pairing", "expected"),
    [
        (connection._BlePairing("failed", "authentication failed"), "rejected the Bluetooth PIN"),
        (connection._BlePairing("failed", "connection attempt failed"), "refused to pair"),
        (connection._BlePairing("failed", "failed: le-connection-abort"), "Linux couldn't pair"),
        (connection._BlePairing("absent"), "Linux couldn't reach"),
        (connection._BlePairing("paired"), "bluetoothctl remove"),
    ],
)
def test_linux_refusals_speak_of_linux_and_bluetoothctl(monkeypatch, pairing, expected) -> None:
    """On Linux the solutions name Linux and ``bluetoothctl``, never the Settings of Windows."""
    monkeypatch.setattr(sys, "platform", "linux")
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR, pin=_A_PIN)
    dev._ble_pairing = pairing
    message, _hint = dev._ble_auth_diagnosis(_BONDED_ADDR, False)
    assert expected in message
    assert "Windows" not in message and "Settings" not in message


def test_a_stale_linux_bond_is_named_as_such(monkeypatch) -> None:
    """If there is no PIN, BlueZ holds a bond, and the device refuses it, the bond is stale.

    The message gives the solution for Linux.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    dev = connection.MeshCoreDevice(transport="ble", address=_BONDED_ADDR)
    message, hint = dev._ble_auth_diagnosis(_BONDED_ADDR, True)
    assert message.startswith("Linux has") and "bluetoothctl remove" in message
    assert "Linux's saved pairing" in hint
    # If there is no bond, the PIN request has no advice that is only for Windows.
    message, _ = dev._ble_auth_diagnosis(_BONDED_ADDR, False)
    assert "--ble-pin" in message and "Windows" not in message


class SerialException(OSError):
    """A stand-in for the exception of pyserial (an ``OSError`` that usually has no ``errno``)."""


@pytest.mark.parametrize(
    ("platform", "exc", "expected"),
    [
        (
            "linux",
            SerialException(
                "[Errno 13] could not open port /dev/ttyACM0: [Errno 13] "
                "Permission denied: '/dev/ttyACM0'"
            ),
            "dialout",
        ),
        (
            "linux",
            SerialException(
                "Could not exclusively lock port /dev/ttyACM0: [Errno 11] "
                "Resource temporarily unavailable"
            ),
            "ModemManager",
        ),
        ("linux", SerialException("[Errno 16] Device or resource busy"), "in use"),
        ("linux", SerialException("[Errno 2] No such file or directory"), "isn't there"),
        (
            "win32",
            SerialException(
                "could not open port 'COM5': PermissionError(13, 'Access is denied.', None, 5)"
            ),
            "in use by another program",
        ),
        (
            "win32",
            SerialException(
                "could not open port 'COM9': FileNotFoundError(2, 'The system "
                "cannot find the file specified.', None, 2)"
            ),
            "isn't there",
        ),
    ],
)
def test_serial_open_failures_are_named(monkeypatch, platform, exc, expected) -> None:
    """A port that does not open says why. It does not say "didn't answer as a MeshCore device"."""
    monkeypatch.setattr(sys, "platform", platform)
    message = connection._serial_open_message("PORT", exc)
    assert message is not None and expected in message
    if platform == "win32":
        assert "dialout" not in message  # On Windows, "Access is denied" is a port that is in use.


def test_an_unrecognised_serial_failure_passes_through() -> None:
    """A failure that is not an open failure keeps its own error."""
    assert connection._serial_open_message("PORT", ValueError("bad baud rate")) is None


async def test_serial_connect_raises_the_named_failure(monkeypatch) -> None:
    """``connect`` turns a refused open into a DeviceCommandError that the picker shows as it is."""
    import types

    class _MeshCore:
        @staticmethod
        async def create_serial(*_args, **_kwargs):
            raise SerialException("[Errno 13] could not open port: Permission denied")

    module = types.ModuleType("meshcore")
    module.MeshCore = _MeshCore
    monkeypatch.setitem(sys.modules, "meshcore", module)
    monkeypatch.setattr(sys, "platform", "linux")
    dev = connection.MeshCoreDevice(port="/dev/ttyACM0")
    with pytest.raises(DeviceCommandError) as excinfo:
        await dev.connect()
    assert "dialout" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, SerialException)


def _fake_meshcore(monkeypatch, **factories) -> None:  # noqa: ANN003
    """Put a ``meshcore`` module in place. Its ``create_*`` factories are the factories given."""
    import types

    module = types.ModuleType("meshcore")
    module.MeshCore = type("MeshCore", (), {k: staticmethod(v) for k, v in factories.items()})
    monkeypatch.setitem(sys.modules, "meshcore", module)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (socket.gaierror(11001, "getaddrinfo failed"), "couldn't find a host named meshbox"),
        (ConnectionRefusedError(111, "Connection refused"), "nothing is accepting connections"),
        (ConnectionResetError(104, "Connection reset by peer"), "one client at a time"),
        (OSError(errno.EHOSTUNREACH, "No route to host"), "no route to meshbox"),
        (OSError(10065, "A socket operation was attempted to an unreachable host"), "no route"),
        (TimeoutError(110, "Connection timed out"), "no answer from meshbox on port 5000"),
        (asyncio.TimeoutError(), "no answer from meshbox"),
    ],
)
def test_tcp_open_failures_are_named(exc: BaseException, expected: str) -> None:
    """Each way in which a socket refuses has its own sentence.

    The sentence is not "no response from a companion".
    """
    message = connection._tcp_open_message("meshbox", 5000, exc)
    assert message is not None and expected in message


def test_an_unrecognised_tcp_failure_is_left_to_the_caller() -> None:
    """A failure that is not a socket failure gets no name, and the caller handles it."""
    assert connection._tcp_open_message("meshbox", 5000, ValueError("odd")) is None


async def test_tcp_connect_raises_the_named_failure(monkeypatch) -> None:
    """``connect`` says that a refused port is a refused port, with the socket error as cause."""

    async def create_tcp(*_args, **_kwargs):
        raise ConnectionRefusedError(111, "Connection refused")

    _fake_meshcore(monkeypatch, create_tcp=create_tcp)
    dev = connection.MeshCoreDevice(transport="tcp", host="10.0.0.9", tcp_port=5000)
    with pytest.raises(DeviceCommandError) as excinfo:
        await dev.connect()
    assert "port 5000 at 10.0.0.9" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ConnectionRefusedError)


async def test_a_tcp_companion_that_never_answers_is_told_from_one_never_reached(
    monkeypatch,
) -> None:
    """A socket that opened and then got silence names the listener.

    The error does not name the address or the network.
    """

    async def create_tcp(*_args, **_kwargs):
        return None  # This is the answer of meshcore when the handshake gets no answer.

    _fake_meshcore(monkeypatch, create_tcp=create_tcp)
    dev = connection.MeshCoreDevice(transport="tcp", host="10.0.0.9", tcp_port=5000)
    with pytest.raises(DeviceCommandError) as excinfo:
        await dev.connect()
    assert "opened, but nothing answered" in str(excinfo.value)


async def test_an_unrecognised_connect_failure_is_said_in_its_own_words(
    monkeypatch, caplog
) -> None:
    """A failure with no name still has a sentence, and MeshTerm logs its traceback one time."""
    monkeypatch.setattr(connection, "_TRACED", set())

    async def create_serial(*_args, **_kwargs):
        raise ValueError("unsupported baud rate")

    _fake_meshcore(monkeypatch, create_serial=create_serial)
    dev = connection.MeshCoreDevice(port="/dev/ttyACM0")
    caplog.set_level("WARNING", logger="meshterm.core.connection")
    for _ in range(2):  # A reconnect that retries against the same fault.
        with pytest.raises(connection.UnrecognisedConnectError) as excinfo:
            await dev.connect()
    assert str(excinfo.value) == (
        "couldn't connect to the device on /dev/ttyACM0: unsupported baud rate"
    )
    assert isinstance(excinfo.value.__cause__, ValueError)
    records = [r for r in caplog.records if "unsupported baud rate" in r.getMessage()]
    assert len(records) == 2
    assert records[0].exc_info is not None  # The first time, the log has the traceback.
    assert records[1].exc_info is None  # After that, the log has only the line.


async def test_a_ble_connect_timeout_is_named_as_a_stall(monkeypatch) -> None:
    """A timeout of the Bluetooth stack says what stopped. It is not a bare ``TimeoutError``."""
    dev = connection.MeshCoreDevice(transport="ble", address=_OPEN_ADDR)

    async def _create_ble(_mesh_core):
        raise TimeoutError

    dev._create_ble = _create_ble  # type: ignore[method-assign]
    _fake_meshcore(monkeypatch)
    with pytest.raises(DeviceCommandError) as excinfo:
        await dev.connect()
    assert "timed out" in str(excinfo.value) and "Move closer" in str(excinfo.value)


async def test_a_tcp_probe_that_runs_out_of_time_says_so() -> None:
    """The time limit of the probe ends before the operating system gives up on a silent host.

    The error names this case too.
    """
    dev = connection.MeshCoreDevice(transport="tcp", host="10.0.0.9", tcp_port=5000)

    async def _connect() -> None:
        raise asyncio.TimeoutError

    dev.connect = _connect  # type: ignore[method-assign]
    with pytest.raises(DeviceCommandError) as excinfo:
        await connection._probe(dev, 1.0)
    assert "no answer from 10.0.0.9 on port 5000" in str(excinfo.value)


async def test_the_probe_names_a_failure_it_does_not_recognise(monkeypatch) -> None:
    """An unusual failure gets its own words. It is not called "not a MeshCore device"."""
    monkeypatch.setattr(connection, "_TRACED", set())
    dev = connection.MeshCoreDevice(port="COM5")

    async def _connect() -> None:
        pass

    async def _self_info() -> dict:
        raise RuntimeError("frame decode failed")

    dev.connect = _connect  # type: ignore[method-assign]
    dev.get_self_info = _self_info  # type: ignore[method-assign]
    with pytest.raises(connection.UnrecognisedConnectError) as excinfo:
        await connection._probe(dev, 1.0)
    assert str(excinfo.value) == (
        "connected to COM5, but reading its identity failed: frame decode failed"
    )


def test_ble_address_int_parses_macs_and_rejects_others() -> None:
    """A MAC with colons or dashes becomes a 48-bit int.

    A non-MAC (for example a macOS UUID) gives None.
    """
    parse = connection.MeshCoreDevice._ble_address_int
    assert parse(_BONDED_ADDR) == 0x001122334455
    assert parse("da-c5-21-6b-7c-7c") == 0xDAC5216B7C7C
    assert parse("not-a-mac") is None
    assert parse("550e8400-e29b-41d4-a716-446655440000") is None  # CoreBluetooth UUID


async def test_ble_pairing_helpers_noop_for_non_mac_address() -> None:
    """The bond query and the unpair stop early for a non-MAC address. They do not touch WinRT."""
    # A non-MAC address fails the parse before any winrt import. Thus these tests stay isolated
    # on each platform, and no real Bluetooth stack is consulted.
    assert await connection.MeshCoreDevice.is_ble_paired("not-a-mac") is False
    assert await connection.MeshCoreDevice.unpair_ble("not-a-mac") is False
    assert await connection.MeshCoreDevice.is_ble_paired("") is False


async def test_can_unpair_only_for_bonded_ble(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quit dialog offers unpair only on a BLE link for which Windows holds a bond."""

    async def _is_paired(address: str) -> bool:
        return address == _BONDED_ADDR

    monkeypatch.setattr(connection.MeshCoreDevice, "is_ble_paired", staticmethod(_is_paired))
    # Serial: the dialog never offers it, for any address.
    serial_ctx = SimpleNamespace(active_transport="serial", active_address=None)
    assert await menu._can_unpair(serial_ctx) is False
    # BLE with no address to act on: the dialog does not offer it.
    ble_no_addr = SimpleNamespace(active_transport="ble", active_address=None)
    assert await menu._can_unpair(ble_no_addr) is False
    # BLE with a live bond: the dialog offers it.
    bonded = SimpleNamespace(active_transport="ble", active_address=_BONDED_ADDR)
    assert await menu._can_unpair(bonded) is True
    # BLE that is open (no bond, for example a companion that has no PIN): the dialog does
    # not offer it.
    open_ble = SimpleNamespace(active_transport="ble", active_address=_OPEN_ADDR)
    assert await menu._can_unpair(open_ble) is False


async def test_unpair_on_exit_disconnects_before_unpairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The teardown drops the live link first, then removes the bond of the operating system.

    The order is important.
    """
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
    # The disconnect comes before the unpair, because a bond that is in use cannot be dropped.
    # Also, MeshTerm releases the device handle. It never touches the device_store, so the
    # stored record stays.
    assert order == ["disconnect", f"unpair:{_BONDED_ADDR}"]
    assert ctx._device is None


def _make_ctx(tmp_path: Path) -> AppContext:
    """Build a real application context, with the simulator as the device, for reconnect tests."""
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
    """Reconnect opens a new connection and resumes the hub, the monitor, and the chat."""
    ctx = _make_ctx(tmp_path)
    try:
        await ctx.events.start()
        await ctx.monitor.start()  # Start to record on the hub that already runs.
        await ctx.chat.start()
        original = await ctx.device()
        assert ctx.events.active and ctx.monitor.active and ctx.chat.active

        await ctx.reconnect()

        # A new connection replaced the dead connection. Each service that ran before the
        # drop runs again.
        rebuilt = await ctx.device()
        assert rebuilt is not original
        assert ctx.events.active
        assert ctx.monitor.active
        assert ctx.chat.active
    finally:
        await ctx.aclose()


async def test_ble_profile_opens_bluetooth_transport(tmp_path: Path, monkeypatch) -> None:
    """A BLE profile makes ``device()`` build a Bluetooth connection with the address."""
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
    # The context module imported make_device by name. Thus the test patches the reference
    # that the context module calls.
    import meshterm.context as context_mod

    monkeypatch.setattr(context_mod, "make_device", fake_make_device)
    try:
        device = await ctx.device()
        assert device.transport == "ble"
        assert built["address"] == "AA:BB:CC:DD:EE:FF"
        assert ctx.active_transport == "ble"
        assert ctx.active_port is None  # BLE has no serial port to watch.
    finally:
        ctx.repo.close()


async def test_tcp_profile_opens_network_transport(tmp_path: Path, monkeypatch) -> None:
    """A TCP profile makes ``device()`` build a network connection with the host and port."""
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
    """Reconnect restores only what was live. Services that were idle stay idle."""
    ctx = _make_ctx(tmp_path)
    try:
        # A tool opened the device when it needed it. No service subscribed.
        original = await ctx.device()

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
    """The intent to resume survives retries. Services restore also if early reconnects fail.

    The test reproduces a bug that happened on real hardware. The first reconnect attempt
    failed and stopped the services. Then later attempts read the flags, which were now
    idle, and restored nothing. The simulator never makes ``device()`` fail. Thus the test
    makes the first two ``device()`` calls raise, as an absent port does. Then the third
    call, and the calls that restore the services, succeed.
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
            if attempts["n"] <= 2:  # On the first two tries, the device is not back yet.
                raise _SerialException("could not open port 'COM_TEST'")
            return await real_device(self)

        monkeypatch.setattr(AppContext, "device", flaky_device)

        # Two reconnects fail (the device is still absent), then one succeeds. This is the
        # same as a slow replug.
        for _ in range(2):
            try:
                await ctx.reconnect()
            except _SerialException:
                pass
        await ctx.reconnect()  # The third device() call succeeds, and the services restore.

        assert ctx.events.active
        assert ctx.monitor.active
        assert ctx.chat.active
    finally:
        monkeypatch.undo()
        await ctx.aclose()


def test_serial_port_present_reflects_os_enumeration(monkeypatch) -> None:
    """A port is 'present' if and only if it is in the list of the operating system.

    This list is the main signal for an unplug.
    """
    pytest.importorskip("serial")
    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", lambda: [SimpleNamespace(device="COM11")])
    assert connection.serial_port_present("COM11")
    assert not connection.serial_port_present("COM99")


def test_serial_port_present_assumes_up_on_enumeration_error(monkeypatch) -> None:
    """A port query that fails must never cause a false disconnect. It reports 'present'."""
    pytest.importorskip("serial")
    from serial.tools import list_ports

    def boom():
        raise OSError("enumeration failed")

    monkeypatch.setattr(list_ports, "comports", boom)
    assert connection.serial_port_present("COM11")


def test_serial_port_present_platform_uart_by_path(monkeypatch) -> None:
    """A platform UART is present if its ``/dev`` node exists. A scan does not decide this.

    ``comports()`` of pyserial does not list a soldered UART (``/dev/ttyS1`` on the Luckfox
    Lyra), but its character-device node stays. Thus a character device that exists is
    present. A node that is gone (a real USB unplug of ``/dev/ttyUSB*``) is absent.
    """
    pytest.importorskip("serial")
    import os as _os
    import stat as _stat

    from serial.tools import list_ports

    # The scan does not list platform UARTs, so it gives [].
    monkeypatch.setattr(list_ports, "comports", list)
    monkeypatch.setattr(_os.path, "exists", lambda p: p == "/dev/ttyS1")
    monkeypatch.setattr(_os, "stat", lambda p: SimpleNamespace(st_mode=_stat.S_IFCHR))

    # A character device that exists is present. A node that is gone is absent (an unplug).
    assert connection.serial_port_present("/dev/ttyS1")
    assert not connection.serial_port_present("/dev/ttyUSB9")


async def test_serial_link_check_walks_the_ports_off_the_event_loop(monkeypatch) -> None:
    """The liveness poll never runs the port walk on the loop from which the screen paints.

    The walk is hundreds of small sysfs reads, and each read waits for its turn at the GIL.
    A worker thread draws a map frame at the same time. When the walk ran on the loop, the
    screen of the PicoCalc froze for up to 1.2 s in each two seconds.
    """
    import threading

    device = connection.MeshCoreDevice(port="/dev/ttyS1")
    device._mc = SimpleNamespace(is_connected=True)
    walked_on: list[threading.Thread] = []

    def walk(port: str) -> bool:
        walked_on.append(threading.current_thread())
        return True

    monkeypatch.setattr(connection, "serial_port_present", walk)
    assert await device.link_present()
    assert walked_on and walked_on[0] is not threading.main_thread()


async def test_wait_for_disconnect_fires_when_port_vanishes(tmp_path: Path, monkeypatch) -> None:
    """The liveness watcher resolves when the port of the connected device leaves the list."""
    ctx = _make_ctx(tmp_path)
    # The test acts as a live session with real hardware on COM_TEST. Nobody can unplug the
    # simulator.
    ctx.mock = False
    ctx._device = _FakeSerialDevice("COM_TEST")  # The device is connected, so is_connected is True.
    ctx._active_transport = "serial"
    ctx._active_port = "COM_TEST"

    checks = {"n": 0}

    def fake_present(port: str) -> bool:
        checks["n"] += 1
        return checks["n"] < 2  # The port is present on the first poll, and gone after that.

    monkeypatch.setattr(connection, "serial_port_present", fake_present)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "_LIVENESS_CONFIRM_S", 0.0)
    try:
        await asyncio.wait_for(menu._wait_for_disconnect(ctx), timeout=2.0)
    finally:
        ctx.repo.close()


async def test_wait_for_disconnect_ignores_the_simulator(tmp_path: Path) -> None:
    """The watcher never fires for --mock, because the simulator has no port to lose."""
    ctx = _make_ctx(tmp_path)  # mock=True
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(menu._wait_for_disconnect(ctx), timeout=0.2)
    finally:
        ctx.repo.close()


async def test_wait_for_disconnect_fires_on_an_announced_reboot(
    tmp_path: Path, monkeypatch
) -> None:
    """A reboot that MeshTerm sent fires the watcher at once, but the port does not go away.

    A board behind a USB-UART bridge keeps its port during a reboot. Thus the poll alone
    does not see the drop. The announcement is the evidence.
    """
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = _FakeSerialDevice("COM_TEST")
    ctx._active_transport = "serial"
    ctx._active_port = "COM_TEST"
    # The port never leaves.
    monkeypatch.setattr(connection, "serial_port_present", lambda port: True)
    try:
        watcher = asyncio.ensure_future(menu._wait_for_disconnect(ctx))
        await asyncio.sleep(0)
        assert not watcher.done()
        ctx.announce_reboot()
        await asyncio.wait_for(watcher, timeout=0.5)  # This is less than one liveness poll.
        assert ctx.take_reboot() is True
        # The first call consumed the reboot, so the next drop is an unplug.
        assert ctx.take_reboot() is False
        with pytest.raises(asyncio.TimeoutError):  # Also, the next watcher waits for one.
            await asyncio.wait_for(menu._wait_for_disconnect(ctx), timeout=0.2)
    finally:
        ctx.repo.close()


async def test_reboot_does_not_wait_for_an_acknowledgement_that_never_comes() -> None:
    """A reboot write that the board never acknowledges returns at once, not at the link loss.

    Over Bluetooth, the write is a write with a response. A board that restarts on the
    command can drop the link before it acknowledges the write. MeshTerm once waited for
    this, and the reboot dialog stayed open until the supervision timeout.
    """
    ended = asyncio.Event()

    async def never_acknowledged() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()

    device = connection.MeshCoreDevice(port="mock")
    device._mc = SimpleNamespace(commands=SimpleNamespace(reboot=never_acknowledged))
    await asyncio.wait_for(device.reboot(), timeout=1.0)
    assert len(connection._REBOOT_WRITES) == 1  # The write is still pending, and a set holds it.
    for write in list(connection._REBOOT_WRITES):
        write.cancel()  # This is the teardown that follows a reboot.
    await asyncio.wait_for(ended.wait(), timeout=1.0)
    await asyncio.sleep(0)
    assert not connection._REBOOT_WRITES


async def test_reboot_still_raises_a_write_that_fails_outright() -> None:
    """The early return is for a write that hangs. A write that fails at once still raises."""

    async def refused() -> None:
        raise RuntimeError("not connected")

    device = connection.MeshCoreDevice(port="mock")
    device._mc = SimpleNamespace(commands=SimpleNamespace(reboot=refused))
    with pytest.raises(RuntimeError):
        await device.reboot()


class _FakeBleDevice:
    """A minimal stand-in for a connected BLE :class:`Device` in the liveness tests.

    Its :meth:`link_present` reads an ``is_connected`` flag. The real BLE device reads the
    connection state of the meshcore client in the same way.
    """

    transport = "ble"

    def __init__(self) -> None:
        self.is_connected = True

    async def link_present(self) -> bool:
        return self.is_connected


async def test_wait_for_disconnect_fires_when_ble_link_drops(tmp_path: Path, monkeypatch) -> None:
    """The same watcher fires for BLE when the connection flag of the peripheral becomes false."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    device = _FakeBleDevice()
    ctx._device = device
    ctx._active_transport = "ble"
    ctx._active_address = "AA:BB:CC:DD:EE:FF"

    async def drop_soon() -> None:
        device.is_connected = False  # The peripheral goes out of range.

    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "_LIVENESS_CONFIRM_S", 0.0)
    try:
        await drop_soon()
        await asyncio.wait_for(menu._wait_for_disconnect(ctx), timeout=2.0)
    finally:
        ctx.repo.close()


class _FakeSession:
    """A minimal stand-in for :class:`TuiSession` that records pushes and pops for dialog tests."""

    def __init__(self) -> None:
        self.stack: list = []
        self.root = None  # No menu declared itself as the root, so the dialog gets a blank base.

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
    """The dialog closes itself, with no key press, when the port of the device appears again."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = object()  # A stand-in for a connected device.
    ctx._active_port = "COM_TEST"

    polls = {"n": 0}

    def fake_present(port: str) -> bool:
        polls["n"] += 1
        return polls["n"] >= 2  # The port is absent on the first poll, and back after that.

    async def fake_reconnect(self: AppContext, *, ble_device: object | None = None) -> None:
        self._device = object()  # A new connection.

    monkeypatch.setattr(connection, "serial_port_present", fake_present)
    monkeypatch.setattr(AppContext, "reconnect", fake_reconnect)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "spinner_interval", lambda: 0.0)

    session = _FakeSession()
    try:
        quit_chosen = await asyncio.wait_for(menu._handle_disconnect(ctx, session), timeout=2.0)
        assert quit_chosen is False  # The app reconnected. The user did not quit.
        assert session.stack == []  # The dialog was removed.
    finally:
        ctx.repo.close()


async def test_handle_disconnect_quits_when_user_presses_quit(tmp_path: Path, monkeypatch) -> None:
    """Enter (the Abort button) quits, also while the device is still gone."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = object()
    ctx._active_port = "COM_TEST"

    # The port never comes back, so the Quit button is the only way out.
    monkeypatch.setattr(connection, "serial_port_present", lambda port: False)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "spinner_interval", lambda: 0.0)

    session = _FakeSession()

    async def press_quit() -> None:
        # When the dialog is on the stack, send Enter to its Abort button.
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
    """Enter aborts (it resolves ``"quit"``). Esc does nothing, so a wrong key press cannot quit."""
    from meshterm.ui.tui import ReconnectDialog

    dialog = ReconnectDialog("Waiting…")
    dialog.future = asyncio.get_running_loop().create_future()

    dialog.handle("escape")
    assert not dialog.future.done()  # Esc does nothing.

    dialog.handle("enter")
    assert dialog.future.result() == "quit"


async def _never() -> None:
    """An awaitable that blocks for ever (a stand-in for an idle worker or watcher)."""
    await asyncio.Event().wait()


async def test_session_loop_quits_without_touching_the_disconnect_path(monkeypatch) -> None:
    """When the menu loop returns (the user quit), the watcher stops and no dialog shows."""
    handled = {"n": 0}

    async def fake_menu(ctx, session):
        return None  # The user quit at the menu.

    async def fake_handle(ctx, session):
        handled["n"] += 1
        return True

    monkeypatch.setattr(menu, "_menu_loop", fake_menu)
    monkeypatch.setattr(menu, "_wait_for_disconnect", lambda ctx: _never())
    monkeypatch.setattr(menu, "_handle_disconnect", fake_handle)

    session = SimpleNamespace(reset=lambda: None)
    await asyncio.wait_for(menu._session_loop(object(), session), timeout=2)
    assert handled["n"] == 0  # The code never entered the disconnect path.


async def test_session_loop_cancels_worker_and_prompts_on_disconnect(monkeypatch) -> None:
    """A disconnect while the menu is busy cancels the worker, clears the stack, and asks."""
    resets = {"n": 0}
    cancelled = {"seen": False}

    async def busy_menu(ctx, session):
        try:
            await asyncio.Event().wait()  # A tool is in progress. The loop must cancel it.
        except asyncio.CancelledError:
            cancelled["seen"] = True
            raise

    async def fires_now(ctx):
        return None  # The port vanished.

    async def quit_at_dialog(ctx, session):
        return True

    monkeypatch.setattr(menu, "_menu_loop", busy_menu)
    monkeypatch.setattr(menu, "_wait_for_disconnect", fires_now)
    monkeypatch.setattr(menu, "_handle_disconnect", quit_at_dialog)

    session = SimpleNamespace(reset=lambda: resets.__setitem__("n", resets["n"] + 1))
    await asyncio.wait_for(menu._session_loop(object(), session), timeout=2)
    assert cancelled["seen"]  # The loop cancelled the worker that was in progress.
    assert resets["n"] == 1  # The loop unwound the stack before the dialog.


async def test_session_loop_resumes_a_fresh_menu_after_reconnect(monkeypatch) -> None:
    """After a reconnect the loop starts a new menu and arms the watcher again."""
    state = {"menu": 0, "watch": 0, "handle": 0}

    async def flaky_menu(ctx, session):
        state["menu"] += 1
        if state["menu"] == 1:
            await asyncio.Event().wait()  # First pass: the disconnect interrupts it.
        return None  # Second pass: the user quits.

    async def watch(ctx):
        state["watch"] += 1
        if state["watch"] == 1:
            return None  # Fire one time.
        await asyncio.Event().wait()  # Never fire again.

    async def reconnected(ctx, session):
        state["handle"] += 1
        return False  # The device came back.

    monkeypatch.setattr(menu, "_menu_loop", flaky_menu)
    monkeypatch.setattr(menu, "_wait_for_disconnect", watch)
    monkeypatch.setattr(menu, "_handle_disconnect", reconnected)

    session = SimpleNamespace(reset=lambda: None)
    await asyncio.wait_for(menu._session_loop(object(), session), timeout=2)
    assert state["handle"] == 1  # The loop handled one disconnect.
    assert state["menu"] == 2  # A new menu ran after the reconnect.


class _WedgedMeshCore:
    """A fake meshcore client whose graceful ``disconnect()`` never returns.

    It reproduces a deadlock of the library when the dispatcher stops. In this deadlock,
    ``queue.join()`` waits for events that are still in the queue after the processor task
    exited. The deadlock once held the exit of MeshTerm until the watchdog killed the
    process. The class records if the forced path ran.
    """

    def __init__(self) -> None:
        self.force_stopped = False
        self.connection_manager = SimpleNamespace(connection=self)
        self.raw_closed = False

    async def disconnect(self) -> None:
        # The code calls this method as the graceful teardown (through the attribute lookup on
        # the client, with no manager) and as the raw transport close. The graceful call
        # hangs. The raw close is the call that comes after the forced stop.
        if not self.force_stopped:
            # This is the deadlock of the dispatcher. It never returns.
            await asyncio.Event().wait()
        self.raw_closed = True

    def stop(self) -> None:
        self.force_stopped = True


async def test_disconnect_bounds_a_wedged_client_teardown(monkeypatch) -> None:
    """MeshTerm abandons a graceful teardown that deadlocks, and closes the transport by force."""
    monkeypatch.setattr(connection, "_DISCONNECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(connection, "_FORCE_DISCONNECT_TIMEOUT_S", 0.5)

    dev = connection.MeshCoreDevice(port="COM_TEST")
    wedged = _WedgedMeshCore()
    dev._mc = wedged

    await asyncio.wait_for(dev.disconnect(), timeout=2.0)  # It must not hang.

    assert wedged.force_stopped  # MeshTerm cancelled the dispatcher task at once.
    assert wedged.raw_closed  # MeshTerm still released the port or link.
    assert dev._mc is None  # A second disconnect does nothing.


async def test_disconnect_graceful_path_needs_no_force(monkeypatch) -> None:
    """A healthy teardown completes gracefully, and the code never enters the forced path."""

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
    """A stand-in for a connected BLE :class:`Device` whose link already dropped."""

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
    """A context that stands in for a live BLE session whose companion just dropped."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = _FakeBleDevice()
    ctx._active_transport = "ble"
    ctx._active_address = "AA:BB:00:00:00:01"
    return ctx


async def test_ble_reconnect_waits_for_the_advertisement_and_hands_over_the_fresh_handle(
    tmp_path: Path, monkeypatch
) -> None:
    """BLE reconnects when it hears the companion, with the handle that the scan found."""
    ctx = _ble_ctx(tmp_path)
    fresh = object()  # The live BLEDevice that the scan produced.
    scans: list[str] = []

    async def fake_find(address: str, timeout: float = 0.0) -> object | None:
        scans.append(address)
        # The companion is silent on the first round, then the scan hears it.
        return fresh if len(scans) >= 2 else None

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
        assert quit_chosen is False  # The app reconnected. The user did not quit.
        # MeshTerm waited until it heard the device. It did not retry the connect blindly.
        assert scans == [ctx._active_address, ctx._active_address]
        assert len(opened) == 1
        # Then MeshTerm opened the device again with the handle that the scan produced. It
        # did not use an old handle.
        assert opened == [fresh]
    finally:
        ctx.repo.close()


async def test_ble_reconnect_releases_the_dead_link_before_scanning(
    tmp_path: Path, monkeypatch
) -> None:
    """MeshTerm closes the old link first, so the peripheral can advertise again."""
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
        assert dead.disconnected  # MeshTerm released the dead companion...
        assert held_at_scan == [False]  # ...before it listened for the companion.
    finally:
        ctx.repo.close()


async def test_reconnect_prefers_the_freshly_scanned_ble_handle(
    tmp_path: Path, monkeypatch
) -> None:
    """A handle that the caller passes to reconnect opens the link.

    MeshTerm does not use the old handle of the startup scan.
    """
    address = "AA:BB:00:00:00:01"
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx.ble_override = address
    stale = object()  # The handle that the scan of the picker produced when the session opened.
    fresh = object()  # The handle of the peripheral that the reconnect flow just heard advertising.
    ctx.selected_device = DiscoveredDevice(
        transport="ble", address=address, name="MeshCore-Homestead", ble_device=stale
    )
    handles: list[object | None] = []

    def fake_make_device(**kwargs: object):  # noqa: ANN202 - a stub that stands in for the radio
        handles.append(kwargs.get("ble_device"))
        return _FakeBleDevice()

    monkeypatch.setattr(context_module, "make_device", fake_make_device)
    try:
        await ctx.reconnect(ble_device=fresh)
        assert handles == [fresh]  # The handle of the new scan, never the old handle.
        assert ctx._ble_handle is None  # The connection that it opened consumed the handle.

        # If the reconnect has no new scan, the handle of the picker is still the best handle.
        await ctx.reconnect()
        assert handles == [fresh, stale]
    finally:
        ctx.repo.close()


async def test_release_link_is_idempotent_and_holds_the_resume_intent(tmp_path: Path) -> None:
    """Two releases are harmless, and reconnect still restores what was running."""
    ctx = _make_ctx(tmp_path)
    try:
        await ctx.events.start()
        await ctx.monitor.start()
        await ctx.release_link()
        assert not ctx.monitor.active  # The services stopped when the link went down.
        await ctx.release_link()  # A second release has nothing to do, and it says nothing.

        await ctx.reconnect()

        # The intent to resume survived both releases.
        assert ctx.events.active and ctx.monitor.active
    finally:
        await ctx.aclose()


class _AbandonedBleakClient:
    """A ``bleak`` client whose peripheral vanished. It is already down, and nobody returns it.

    The meshcore transport leaves this client when a link drops. ``is_connected`` is
    already ``False``. Thus each guarded teardown in the library does not touch the client.
    """

    def __init__(self) -> None:
        self.is_connected = False
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class _DroppedBleConnection:
    """The ``BLEConnection`` of meshcore after its callback for a dropped link ran.

    The reference to the client is gone.
    """

    def __init__(self) -> None:
        self.client = None  # handle_disconnect set this to the value that the constructor had.
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1  # This guards on self.client, so it reaches nothing.


class _DroppedMeshCore:
    """A meshcore client whose connection manager also believes that it is already disconnected."""

    def __init__(self, connection: _DroppedBleConnection) -> None:
        self.connection_manager = SimpleNamespace(connection=connection)
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True  # This guards on _is_connected, so it reaches nothing.

    def stop(self) -> None:
        pass


async def test_a_vanished_peripheral_still_gets_its_bleak_client_closed() -> None:
    """MeshTerm releases the client, also if each guard of the library says nothing is open."""
    dev = connection.MeshCoreDevice(transport="ble", address="AA:BB:00:00:00:01")
    dropped = _DroppedBleConnection()
    dev._mc = _DroppedMeshCore(dropped)
    abandoned = _AbandonedBleakClient()
    dev._ble_client = abandoned  # The client that MeshTerm kept at the connect step.

    await dev.disconnect()

    # The teardown of the library reached nothing, because the connection already let the
    # client go.
    assert dropped.client is None
    # The teardown of MeshTerm closed the client in spite of this. This releases the WinRT
    # handles that the next connect needs.
    assert abandoned.disconnect_calls == 1
    assert dev._ble_client is None  # MeshTerm does not close the client two times.
    assert dev._mc is None


async def test_releasing_the_bleak_client_is_idempotent_and_never_raises() -> None:
    """A second disconnect has nothing to do, and MeshTerm ignores a close that fails."""
    dev = connection.MeshCoreDevice(transport="ble", address="AA:BB:00:00:00:01")

    await dev.disconnect()  # Nothing is connected.
    assert dev._ble_client is None

    class _RefusesToClose:
        is_connected = False

        async def disconnect(self) -> None:
            raise OSError("the handle is invalid")

    dev._ble_client = _RefusesToClose()
    await dev.disconnect()  # It must not raise, because the teardown is a best effort.
    assert dev._ble_client is None


async def test_a_healthy_bluetooth_teardown_still_releases_the_client() -> None:
    """The graceful path runs as before, and MeshTerm releases the client after it."""
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
    assert not healthy.force_stopped  # The graceful path was enough, as it always was.
    assert live.disconnect_calls == 1  # Also, MeshTerm released the client in each case.


# --- What the user sees when a connection does not open ----------------------------------------


def _refusal() -> Exception:
    """A connect failure as the context raises it: its own wrapper around the named sentence."""
    from meshterm.core.selection import DeviceSelectionError

    try:
        try:
            raise DeviceCommandError(
                "COM_TEST is in use by another program — the MeshCore app, a flasher, or a "
                "serial monitor. Close it and try again."
            )
        except DeviceCommandError as cause:
            raise DeviceSelectionError(f"could not open serial port COM_TEST: {cause}") from cause
    except DeviceSelectionError as exc:
        return exc


async def test_a_device_that_failed_to_open_is_not_kept(tmp_path: Path, monkeypatch) -> None:
    """After an open that failed, nothing claims to be connected, and the next caller tries again.

    The device once stayed in the context without an open connection. ``is_connected`` said
    yes. Each tool that ran after the failed start got this device, and it failed with "not
    connected" and not with the reason.
    """
    from meshterm.core.selection import DeviceSelectionError

    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx.tcp_override = "10.0.0.9:5000"
    built: list = []

    class _Refusing:
        transport = "tcp"
        disconnects = 0

        async def connect(self) -> None:
            raise DeviceCommandError("nothing is accepting connections on port 5000 at 10.0.0.9")

        async def disconnect(self) -> None:
            self.disconnects += 1

    def fake_make_device(**_kw):
        built.append(_Refusing())
        return built[-1]

    monkeypatch.setattr(context_module, "make_device", fake_make_device)
    try:
        for attempt in (1, 2):
            with pytest.raises(DeviceSelectionError, match="port 5000"):
                await ctx.device()
            assert not ctx.is_connected
            assert len(built) == attempt  # Each time is a new attempt. It is not the dead device.
        assert built[0].disconnects == 1  # MeshTerm closed the part of it that opened.
    finally:
        ctx.repo.close()


def test_a_failure_reads_as_the_connection_s_own_sentence() -> None:
    """The dialog shows the named sentence, not the wrapper that repeats the endpoint."""
    from meshterm.ui.device_picker import connect_failure_text

    text = connect_failure_text(_refusal())
    assert text.plain.startswith("COM_TEST is in use by another program")
    assert "could not open serial port" not in text.plain
    assert "The full error" not in text.plain  # The failure has a name, so the log has no more.


@pytest.mark.parametrize(
    ("sentence", "shown"),
    [
        ("couldn't open a Bluetooth link to X", "Couldn't open a Bluetooth link to X"),
        ("no permission to open COM5.", "No permission to open COM5."),
        ("meshbox.local:5000 closed the connection", "meshbox.local:5000 closed the connection"),
        ("/dev/ttyACM0 isn't there", "/dev/ttyACM0 isn't there"),
        ("c0:ff:ee:00:00:01 rejected the PIN", "c0:ff:ee:00:00:01 rejected the PIN"),
    ],
)
def test_a_dialog_capitalises_a_plain_word_and_never_a_name(sentence: str, shown: str) -> None:
    """A dialog capitalizes the lowercase sentence of the command line, but never a name.

    A sentence that starts with a port, a host, or an address keeps that spelling, because
    the user must match it with their own spelling.
    """
    from meshterm.ui.device_picker import connect_failure_text

    assert connect_failure_text(DeviceCommandError(sentence)).plain == shown


def test_an_unrecognised_failure_says_where_the_log_is(monkeypatch, tmp_path: Path) -> None:
    """Only a failure that MeshTerm cannot name points to the log, and only if the log has it."""
    from meshterm.ui import device_picker

    log = tmp_path / "meshterm.log"
    odd = connection.UnrecognisedConnectError("couldn't connect to COM5: [WinError 31] odd")
    monkeypatch.setattr(device_picker, "log_file_for", lambda level: log)
    text = device_picker.connect_failure_text(odd)
    assert text.plain == f"Couldn't connect to COM5: [WinError 31] odd\nThe full error is in {log}"

    # The log level is ERROR.
    monkeypatch.setattr(device_picker, "log_file_for", lambda level: None)
    assert "The full error" not in device_picker.connect_failure_text(odd).plain


async def test_reconnect_says_why_once_the_failure_persists(tmp_path: Path, monkeypatch) -> None:
    """A device that is back but refuses gets its reason under the spinner, not a silent wait.

    MeshTerm ignores the first failure, because a board can refuse one time while it boots.
    Thus the reason appears from the second failure.
    """
    from rich.text import Text

    from meshterm.ui.tui import ReconnectDialog

    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = object()
    ctx._active_port = "COM_TEST"
    dialog = ReconnectDialog("Waiting for your device — reconnect it to resume.")
    dialog.future = asyncio.get_running_loop().create_future()
    seen: list[str] = []

    def body() -> str:
        return Text.from_ansi("\n".join(dialog.render_body(68))).plain

    async def fake_reconnect(self: AppContext, *, ble_device: object | None = None) -> None:
        seen.append(body())
        if len(seen) <= 2:
            raise _refusal()

    async def fake_release(self: AppContext) -> None:
        pass

    monkeypatch.setattr(connection, "serial_port_present", lambda port: True)
    monkeypatch.setattr(AppContext, "reconnect", fake_reconnect)
    monkeypatch.setattr(AppContext, "release_link", fake_release)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    try:
        await asyncio.wait_for(menu._auto_reconnect(ctx, dialog), timeout=2.0)
        assert dialog.future.result() == "reconnected"
        assert "in use by another program" not in seen[1]  # After one failure, it only retried.
        assert "in use by another program" in seen[2]  # After two failures, the dialog says why.
        assert "Still trying to reconnect" in seen[2]
    finally:
        ctx.repo.close()


async def test_reconnect_forgets_the_reason_when_the_device_goes_again(
    tmp_path: Path, monkeypatch
) -> None:
    """If the user unplugs the device after it refused, MeshTerm waits for it again."""
    from rich.text import Text

    from meshterm.ui.tui import ReconnectDialog

    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = object()
    ctx._active_port = "COM_TEST"
    dialog = ReconnectDialog("Waiting for your device — reconnect it to resume.")
    dialog.future = asyncio.get_running_loop().create_future()
    # The device refuses two times, the user pulls it, and it comes back.
    present = iter([True, True, False, True])
    bodies: list[str] = []

    def port_present(_port: str) -> bool:
        bodies.append(Text.from_ansi("\n".join(dialog.render_body(68))).plain)
        return next(present)

    attempts = {"n": 0}

    async def fake_reconnect(self: AppContext, *, ble_device: object | None = None) -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise _refusal()

    async def fake_release(self: AppContext) -> None:
        pass

    monkeypatch.setattr(connection, "serial_port_present", port_present)
    monkeypatch.setattr(AppContext, "reconnect", fake_reconnect)
    monkeypatch.setattr(AppContext, "release_link", fake_release)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    try:
        await asyncio.wait_for(menu._auto_reconnect(ctx, dialog), timeout=2.0)
        assert "in use by another program" in bodies[2]  # The dialog says why, while it refuses.
        # The device is gone again, so MeshTerm waits.
        assert "in use by another program" not in bodies[3]
        assert "Waiting for your device" in bodies[3]
    finally:
        ctx.repo.close()


async def test_a_radio_that_will_not_start_is_reported_before_the_menu(
    tmp_path: Path, monkeypatch
) -> None:
    """If the command line names the device, there is no picker, so the startup says why."""
    ctx = _make_ctx(tmp_path)
    notes: list = []

    async def failing_start() -> None:
        raise _refusal()

    async def notify_startup(renderable, *, title="", banner=None, footnote=None):
        notes.append((title, renderable.plain))

    monkeypatch.setattr(ctx.events, "start", failing_start)
    monkeypatch.setattr(menu, "_warm_basemap", lambda _ctx: None)
    monkeypatch.setattr(ctx, "ui", SimpleNamespace(notify_startup=notify_startup))
    try:
        failure = await menu._resume_monitor(ctx)
        assert failure is not None
        assert not ctx.chat.active  # There is no second try at the same refusal.
        await menu._report_startup_failure(ctx, failure)
        [(title, plain)] = notes
        assert title == "Can't connect yet"
        assert plain.startswith("COM_TEST is in use by another program")
        assert "The menu opens without a radio" in plain
    finally:
        await ctx.aclose()


class _ToolUi:
    """Collects what a run of a tool leaves for its result dialog."""

    def __init__(self) -> None:
        self.buffer: list = []
        self.presented = False

    def note(self, markup: str) -> None:
        from rich.text import Text

        self.buffer.append(Text.from_markup(markup))

    def show(self, *renderables) -> None:  # noqa: ANN002
        self.buffer.extend(renderables)

    def discard(self) -> None:
        self.buffer = []

    async def present(self, *, title: str = "") -> None:
        self.presented = True


async def _run_failing_tool(tmp_path: Path, monkeypatch, exc: Exception) -> _ToolUi:
    """Run a menu tool that raises ``exc``, and return what the tool left to show."""
    from meshterm import tools

    class _Tool:
        name = title = "Contacts"

        async def prompt_params(self, _ctx):
            raise exc

    ctx = _make_ctx(tmp_path)
    ui = _ToolUi()
    monkeypatch.setattr(tools, "get_tool", lambda name: _Tool())
    monkeypatch.setattr(ctx, "ui", ui)
    try:
        await menu._run_selection(ctx, "contacts")
    finally:
        ctx.repo.close()
    return ui


async def test_a_tool_whose_radio_never_opened_says_why(tmp_path: Path, monkeypatch) -> None:
    """A connect that failed shows the sentence of the connection, also for a lost link.

    The watcher has no connected device to notice, so no other code says the reason.
    """
    from meshterm.core.selection import DeviceSelectionError

    try:
        raise DeviceSelectionError("could not open BLE companion X") from OSError(
            "input/output error"
        )
    except DeviceSelectionError as raised:
        exc = raised
    assert is_connection_lost(exc)
    ui = await _run_failing_tool(tmp_path, monkeypatch, exc)
    assert ui.presented
    assert [t.plain for t in ui.buffer] == ["⚠ Could not open BLE companion X"]


async def test_a_tool_failure_shows_the_error_s_brackets(tmp_path: Path, monkeypatch) -> None:
    """The bracketed words of a library are shown. The markup does not take them as a tag."""
    exc = RuntimeError("[org.bluez.Error.Failed] le-connection-abort-by-local")
    ui = await _run_failing_tool(tmp_path, monkeypatch, exc)
    [text] = ui.buffer
    assert text.plain == "⚠ Contacts failed: [org.bluez.Error.Failed] le-connection-abort-by-local"
