# SPDX-License-Identifier: Apache-2.0
"""Tests for the BLE connect path's ownership of its meshcore client.

The bug that these tests guard against: ``MeshCore.create_ble`` builds a client, calls
``connect()`` on it, and returns it only if the connect succeeds. A connect that raises is
exactly how a PIN-protected companion answers a notify-subscribe that has no bond. In that
case, the client and the bleak link that it already opened stayed in the library, with no
owner. The ``disconnect`` of MeshTerm did nothing (``_mc`` was never assigned). Windows kept
the ACL link for the life of the process. The peripheral still believed that it had a peer,
and it stopped advertising. The PIN dialog that opened next then asked for a code that it
could not deliver any more.

To own the client is only half of the fix, and the fakes here model the other half. The
``ConnectionManager.disconnect`` of the library closes its transport only ``if
self._is_connected``. The library sets this flag after ``connection.connect()`` returns. A
connect that raised never set it. Thus a graceful ``mc.disconnect()`` leaves a live
``BleakClient`` and believes that nothing is open. For this reason ``_discard_meshcore`` also
closes the transport directly. Also for this reason, the assertions below are about the
transport, and not about a call to ``mc.disconnect``.

All tests run against stand-ins for ``meshcore.MeshCore`` and ``meshcore.BLEConnection``.
They use no radio, no real address, and no real pairing code.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from meshterm.core import connection as conn_mod
from meshterm.core.connection import MeshCoreDevice

# A stand-in address. It is not the address of a real companion on purpose. A test that
# names a real address puts a personal device in the repository, and no test here needs
# hardware.
_ADDR = "00:11:22:33:44:55"


class _FakeTransport:
    """A stand-in for ``meshcore.BLEConnection``, which holds the bleak client.

    ``link_open`` is the important attribute. It stands for the link at the operating-system
    level. If the link stays open, the peripheral believes that it still has a peer, and it
    stops advertising.
    """

    last: _FakeTransport | None = None

    def __init__(self, address=None, device=None, pin=None) -> None:
        self.address = address
        self.device = device
        self.pin = pin
        self.link_open = False
        self.disconnect_calls = 0
        #: The answer of the peripheral to a write: ``None`` accepts it, an exception refuses it.
        self.write_answer: BaseException | None = None
        _FakeTransport.last = self

    async def _write_locked(self, data) -> None:
        if self.write_answer is not None:
            raise self.write_answer

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.link_open = False


class _FakeConnectionManager:
    """Copies the guard of the library: it does not close a transport that it never saw connect."""

    def __init__(self, connection: _FakeTransport) -> None:
        self.connection = connection
        self._is_connected = False

    async def disconnect(self) -> None:
        if self._is_connected:  # the trap: False if connection.connect() raised
            await self.connection.disconnect()
            self._is_connected = False


class _FakeMeshCore:
    """A stand-in for the ``meshcore.MeshCore`` class, with one instance for each connect attempt.

    ``outcomes`` controls each ``connect()`` in turn:

    * An exception instance is raised. The link is already up, as the real GATT failure
      leaves it.
    * ``"none"`` returns ``None`` (the transport is up, with no identity reply).
    * ``"slow"`` hangs, so that the caller can cancel it.
    * A ``("refused", exc)`` pair makes the write of the handshake fail with ``exc``. The
      fake swallows the failure, as ``send`` of meshcore does, and the handshake is empty.
    * Any other value is returned as a successful handshake.
    """

    #: Each instance that a test built, in order (a retry builds a second one).
    built: list[_FakeMeshCore] = []
    #: Each connect attempt uses one entry.
    outcomes: list = []

    def __init__(self, cx, *, default_timeout=None, auto_reconnect=False, **kwargs) -> None:
        self.cx = cx
        self.connection_manager = _FakeConnectionManager(cx)
        self.default_timeout = default_timeout
        self.auto_reconnect = auto_reconnect
        self.graceful_calls = 0
        self.stop_calls = 0
        type(self).built.append(self)

    async def connect(self):
        outcome = type(self).outcomes.pop(0)
        self.cx.link_open = True  # bleak is connected when anything below fails
        if isinstance(outcome, BaseException):
            # The GATT subscribe fails here, after the link is up and before the manager
            # records it. Nothing after this point closes the link, unless a caller
            # reaches the cx.
            raise outcome
        if isinstance(outcome, tuple) and outcome[0] == "refused":
            self.cx.write_answer = outcome[1]
            try:  # send of meshcore: log the failure, return False, and continue
                await self.cx._write_locked(b"")
            except Exception:
                pass
            return None
        if outcome == "slow":
            await asyncio.sleep(30)  # a handshake that the wait_for of the caller cancels
        self.connection_manager._is_connected = True
        return None if outcome == "none" else {"ok": True}

    async def disconnect(self) -> None:
        self.graceful_calls += 1
        await self.connection_manager.disconnect()

    def stop(self) -> None:
        self.stop_calls += 1

    @classmethod
    def reset(cls, *outcomes) -> None:
        cls.built = []
        cls.outcomes = list(outcomes)


@pytest.fixture(autouse=True)
def _fake_meshcore(monkeypatch: pytest.MonkeyPatch):
    """Make ``from meshcore import BLEConnection`` in the connect path give a fake."""
    import sys
    import types

    module = types.ModuleType("meshcore")
    module.BLEConnection = _FakeTransport
    module.MeshCore = _FakeMeshCore
    monkeypatch.setitem(sys.modules, "meshcore", module)
    _FakeTransport.last = None
    yield


def _device(pin: str | None = None) -> MeshCoreDevice:
    return MeshCoreDevice(transport="ble", address=_ADDR, pin=pin, connect_timeout=5.0)


class _AuthError(Exception):
    """A stand-in for the GATT failure that a PIN-protected companion gives."""


def test_a_raising_connect_releases_the_link_it_left_open() -> None:
    """The main bug: a connect that raises must not leave an open link with no owner.

    ``create_ble`` would have lost the reference here. When MeshTerm owns the client, the
    failure path closes the link before the exception continues. Thus the peripheral does
    not keep a phantom peer, and it continues to advertise for the PIN retry.
    """
    _FakeMeshCore.reset(_AuthError("Insufficient Authentication"))
    dev = _device()

    with pytest.raises(_AuthError):
        asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == 1
    transport = _FakeMeshCore.built[0].cx
    assert transport.link_open is False, "the link was left up — the peripheral goes deaf"
    assert transport.disconnect_calls == 1


def test_the_graceful_disconnect_alone_would_not_have_been_enough() -> None:
    """The regression guard for the first attempt at this fix, which still leaked.

    ``ConnectionManager.disconnect`` does nothing after a connect that raised. It checks a
    flag, and the library sets the flag only after ``connection.connect()`` returns. A call
    to ``mc.disconnect`` that stops there looks like a teardown, but it closes nothing. This
    test makes sure that the graceful call has no effect on this path, and that the link
    closed in another way.
    """
    _FakeMeshCore.reset(_AuthError("Insufficient Authentication"))
    dev = _device()

    with pytest.raises(_AuthError):
        asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    client = _FakeMeshCore.built[0]
    assert client.graceful_calls == 1  # the code tried it…
    assert client.connection_manager._is_connected is False  # …and the guard refused it
    assert client.cx.link_open is False  # …so the direct close of the transport closed the link
    assert client.stop_calls == 1  # also, the code cancelled the dispatcher and did not await it


def test_an_unanswered_handshake_closes_the_link_too() -> None:
    """The transport is up but no identity reply comes. MeshTerm reports "not a companion".

    The link still closes.
    """
    _FakeMeshCore.reset("none")
    dev = _device()

    assert asyncio.run(dev._connect_owned_ble(_FakeMeshCore)) is None
    assert _FakeMeshCore.built[0].cx.link_open is False


def test_a_good_connect_hands_the_client_over_still_open() -> None:
    """The success path must not close anything, because the caller owns the live client."""
    _FakeMeshCore.reset("ok")
    dev = _device()

    client = asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert client is _FakeMeshCore.built[0]
    assert client.graceful_calls == 0
    assert client.cx.link_open is True


def test_the_connection_is_built_from_this_device_s_own_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The address and the discovered ``BLEDevice`` reach the new connection. The PIN does not."""
    monkeypatch.setattr(sys, "platform", "win32")
    _FakeMeshCore.reset("ok")
    sentinel = object()
    dev = MeshCoreDevice(
        transport="ble",
        address=_ADDR,
        pin="000000",
        ble_device=sentinel,
        connect_timeout=7.5,
    )

    asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    cx = _FakeTransport.last
    assert (cx.address, cx.device, cx.pin) == (_ADDR, sentinel, None)
    client = _FakeMeshCore.built[0]
    # auto_reconnect stays off, because MeshTerm controls the reconnection itself.
    assert (client.default_timeout, client.auto_reconnect) == (7.5, False)


def test_the_pin_is_withheld_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """MeshTerm gives no PIN to macOS, because a PIN breaks the connection there.

    When it gets a PIN, ``BLEConnection.connect`` calls ``pair()`` of bleak. CoreBluetooth
    has no pairing API, so the macOS backend raises an error at once. Then the library
    disconnects and raises the error again. On macOS, the operating system does the
    pairing, and the subscribe without a bond starts it. Thus a protected companion can bond
    only if MeshTerm gives no PIN.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    _FakeMeshCore.reset("ok")
    dev = MeshCoreDevice(transport="ble", address=_ADDR, pin="000000")

    asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert _FakeTransport.last.pin is None
    assert dev._pin == "000000"  # the device still remembers it, because the platform cannot use it


def test_the_pin_is_withheld_from_bleak_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux, MeshTerm pairs through its own BlueZ agent, so bleak never gets the PIN.

    The BlueZ ``pair()`` of bleak ignores the PIN. It pairs through the system agent that is
    present, which is a desktop dialog or nothing. Thus a PIN given to it would only start a
    second pairing that nobody can answer.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    _FakeMeshCore.reset("ok")
    dev = MeshCoreDevice(transport="ble", address=_ADDR, pin="000000")

    asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert _FakeTransport.last.pin is None
    assert dev._pin == "000000"


def test_the_pin_is_withheld_from_bleak_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The WinRT ``pair()`` of bleak is Just Works only, so a PIN given to it bonds without a PIN.

    The PIN pairing of MeshTerm once failed because of a wrong PIN. Then that fallback left
    Windows with a bond that had no authentication. The next attempt had the right PIN, but
    it used the old bond, and the companion refused it.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    _FakeMeshCore.reset("ok")
    dev = MeshCoreDevice(transport="ble", address=_ADDR, pin="000000")

    asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert _FakeTransport.last.pin is None
    assert dev._pin == "000000"


def test_a_refused_handshake_write_is_a_pairing_problem() -> None:
    """A firmware that guards only the write (MeshOS, wadamesh on a T-Deck) still needs pairing.

    On this firmware the subscribe succeeds, and the refusal comes on the first write of the
    handshake. ``send`` of meshcore swallows that refusal. The empty handshake must show as
    that refusal, and not as "not a companion". Then the pairing, the repair of a stale bond,
    and the macOS wait all run.
    """
    refusal = _AuthError("GATT Protocol Error: Insufficient Encryption")
    _FakeMeshCore.reset(("refused", refusal))
    dev = _device()

    with pytest.raises(_AuthError) as caught:
        asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert caught.value is refusal
    assert _FakeMeshCore.built[0].cx.link_open is False  # released before it was raised


def test_a_write_failing_for_another_reason_still_reads_as_no_answer() -> None:
    """Only a refusal because of a missing bond is promoted. A dropped write stays "no answer"."""
    _FakeMeshCore.reset(("refused", OSError("device unreachable")))
    dev = _device()

    assert asyncio.run(dev._connect_owned_ble(_FakeMeshCore)) is None


def test_a_refused_write_heals_through_the_pin_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end on Windows: refused write → unpair and pair with the PIN → connected.

    This is the exact case of the T-Deck. The fast path trusts a Just Works bond, and the
    companion refuses the write of the handshake over that bond. Stock firmware reaches the
    repair from the subscribe. The code must also reach the repair from the write.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    forced: list[bool] = []

    async def _pair(self, *, force: bool) -> bool:
        forced.append(force)
        return True

    monkeypatch.setattr(MeshCoreDevice, "_pair_ble", _pair)
    _FakeMeshCore.reset(("refused", _AuthError("Insufficient Authentication")), "ok")
    dev = _device(pin="000000")

    client = asyncio.run(dev._create_ble(_FakeMeshCore))

    assert client is _FakeMeshCore.built[1]
    assert forced == [False, True]  # the fast path, then the repair


def test_a_cancelled_handshake_still_closes_the_link() -> None:
    """A ``wait_for`` of a probe that expires during the handshake must not leak what it cancelled.

    This is the second way to this fault, and the reason that the teardown is shielded. A
    plain ``await`` would be cancelled when it suspended, and the close would not run.
    """
    _FakeMeshCore.reset("slow")
    dev = _device()

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(dev._connect_owned_ble(_FakeMeshCore), timeout=0.1)
        # The shielded close continues after the cancellation propagates. Give it a tick.
        await asyncio.sleep(0.2)
        return _FakeMeshCore.built[0]

    client = asyncio.run(scenario())
    assert client.cx.link_open is False, "a cancelled handshake stranded its link"


def test_a_failing_teardown_never_masks_the_real_error() -> None:
    """The teardown is best-effort. The caller must see the failure of the connect itself."""

    class _Stubborn(_FakeMeshCore):
        async def disconnect(self):
            self.graceful_calls += 1
            raise RuntimeError("teardown exploded")

    _Stubborn.reset(_AuthError("Insufficient Authentication"))
    dev = _device()

    with pytest.raises(_AuthError):
        asyncio.run(dev._connect_owned_ble(_Stubborn))
    # The graceful half failed with an error, and the forced close still ran and closed the link.
    assert _Stubborn.built[0].cx.link_open is False


def test_a_wedged_teardown_is_not_waited_out_forever() -> None:
    """A stop of the dispatcher that deadlocks is abandoned, and the transport closes anyway."""

    class _Wedged(_FakeMeshCore):
        async def disconnect(self):
            self.graceful_calls += 1
            await asyncio.sleep(30)  # the deadlock of queue.join() in the library

    _Wedged.reset(_AuthError("Insufficient Authentication"))
    dev = _device()

    async def scenario():
        with pytest.raises(_AuthError):
            await dev._connect_owned_ble(_Wedged)
        return _Wedged.built[0]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(conn_mod, "_DISCARD_TIMEOUT_S", 0.1)
        client = asyncio.run(scenario())
    assert client.cx.link_open is False


def test_the_retry_loop_never_reuses_a_torn_down_client() -> None:
    """A retry of the link-open builds a new client, and MeshTerm released the abandoned one."""
    _FakeMeshCore.reset(ConnectionError("Failed to connect to device"), "ok")
    dev = _device()

    client = asyncio.run(dev._create_ble_with_retry(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == 2, "the retry did not build its own client"
    assert _FakeMeshCore.built[0].cx.link_open is False, "the failed attempt leaked"
    assert client is _FakeMeshCore.built[1]
    assert client.cx.link_open is True


def test_every_link_open_attempt_failing_raises_the_last_error() -> None:
    """When all the retries fail, MeshTerm says that the link never opened. Nothing stays open."""
    _FakeMeshCore.reset(
        ConnectionError("Failed to connect to device"),
        ConnectionError("Failed to connect to device"),
    )
    dev = _device()

    with pytest.raises(conn_mod.DeviceCommandError) as excinfo:
        asyncio.run(dev._create_ble_with_retry(_FakeMeshCore))

    assert isinstance(excinfo.value.__cause__, ConnectionError)
    assert len(_FakeMeshCore.built) == conn_mod._BLE_CONNECT_ATTEMPTS
    assert all(not c.cx.link_open for c in _FakeMeshCore.built)


class _BleakError(Exception):
    """A stand-in for ``bleak.exc.BleakError``, which has only text to say what happened."""


def test_a_link_that_drops_while_connecting_is_retried() -> None:
    """If BlueZ loses the peer during service discovery, the radio caused it, so MeshTerm retries.

    Seen on a Cardputer Zero: an HCI Connection Timeout 1.7 s after the start, before any
    security exchange began. The next attempt connected.
    """
    _FakeMeshCore.reset(_BleakError("failed to discover services, device disconnected"), "ok")
    dev = _device()

    client = asyncio.run(dev._create_ble_with_retry(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == 2
    assert _FakeMeshCore.built[0].cx.link_open is False, "the dropped attempt leaked"
    assert client is _FakeMeshCore.built[1]


def test_an_auth_refusal_that_ends_in_a_disconnect_is_never_retried() -> None:
    """A refused PIN can also drop the link. That case goes to the PIN handling, not to a retry."""
    _FakeMeshCore.reset(_AuthError("Insufficient Authentication: device disconnected"), "ok")
    dev = _device()

    with pytest.raises(_AuthError):
        asyncio.run(dev._create_ble_with_retry(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == 1


def test_any_other_failure_is_not_retried() -> None:
    """Only a link that never opened or that dropped is a radio problem.

    MeshTerm raises all other failures at once.
    """
    _FakeMeshCore.reset(_BleakError("Characteristic 6e400003 was not found"), "ok")
    dev = _device()

    with pytest.raises(_BleakError):
        asyncio.run(dev._create_ble_with_retry(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == 1


class _Connection:
    """A stand-in for ``BLEConnection``, with only the drop handler that the guard wraps."""

    def __init__(self) -> None:
        self.forwarded: list[object] = []

    def handle_disconnect(self, client: object) -> None:
        self.forwarded.append(client)


def test_a_hang_up_after_the_pin_was_asked_is_flagged_not_held() -> None:
    """The code holds back the retries that bleak makes, but not a refusal to pair."""
    asked = False
    connection = _Connection()
    seen = conn_mod._hold_disconnects_while_connecting(connection, lambda: asked)

    connection.handle_disconnect("first try")  # a 0x3e retry of bleak: held, with no flag
    assert not seen.hung_up.is_set()
    asked = True
    connection.handle_disconnect("after the PIN")
    assert seen.hung_up.is_set()
    assert connection.forwarded == []  # still held during the connect


def test_a_hang_up_ends_the_connect_as_a_pairing_failure() -> None:
    """The connect stops waiting for a dead link and says why, in the words of the PIN handling."""

    async def scenario() -> bool:
        hung_up = asyncio.Event()
        cancelled = asyncio.Event()

        async def connect() -> str:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "never"

        asyncio.get_running_loop().call_later(0.05, hung_up.set)
        with pytest.raises(conn_mod._PairingHungUp) as excinfo:
            await conn_mod._unless_hung_up(connect(), hung_up)
        assert conn_mod._is_ble_auth_error(excinfo.value)
        return cancelled.is_set()

    assert asyncio.run(scenario()), "the abandoned connect was left running"


def test_a_connect_that_finishes_first_is_returned() -> None:
    """With no hang-up, nothing changes: the function returns the answer of the connect itself."""

    async def scenario() -> str:
        async def connect() -> str:
            return "started"

        return await conn_mod._unless_hung_up(connect(), asyncio.Event())

    assert asyncio.run(scenario()) == "started"
