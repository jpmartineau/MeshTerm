# SPDX-License-Identifier: Apache-2.0
"""Tests for the BLE connect path's ownership of its meshcore client.

The bug these pin down: ``MeshCore.create_ble`` builds a client, calls ``connect()`` on it,
and returns it *only on success*. A connect that raises — which is exactly how a
PIN-protected companion answers an unbonded notify-subscribe — therefore left the client,
and the bleak link it had already opened, orphaned inside the library. MeshTerm's own
``disconnect`` was a no-op (``_mc`` was never assigned), Windows held the ACL link for the
life of the process, and the peripheral, still believing it had a peer, stopped advertising.
The PIN dialog that opened next then asked for a code it could no longer deliver.

Owning the client is only half of it, and the fakes here model the other half. The library's
``ConnectionManager.disconnect`` closes its transport only ``if self._is_connected`` — a flag
set *after* ``connection.connect()`` returns. A connect that raised never set it, so a
graceful ``mc.disconnect()`` walks away from a live ``BleakClient`` believing there is
nothing to close. That is why ``_discard_meshcore`` also closes the transport directly, and
why the assertions below are about the *transport*, not about ``mc.disconnect`` being called.

Everything runs against stand-ins for ``meshcore.MeshCore`` and ``meshcore.BLEConnection``;
no radio, no real address, and no real pairing code is involved.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from meshterm.core import connection as conn_mod
from meshterm.core.connection import MeshCoreDevice

# A stand-in address. Deliberately not a real companion's: a test that names one turns a
# personal device into repository content, and nothing here needs hardware to run.
_ADDR = "00:11:22:33:44:55"


class _FakeTransport:
    """Stands in for ``meshcore.BLEConnection`` — what actually holds the bleak client.

    ``link_open`` is the thing that matters: it stands for the OS-level link that, left up,
    makes the peripheral believe it still has a peer and stop advertising.
    """

    last: _FakeTransport | None = None

    def __init__(self, address=None, device=None, pin=None) -> None:
        self.address = address
        self.device = device
        self.pin = pin
        self.link_open = False
        self.disconnect_calls = 0
        #: What the peripheral answers a write with: ``None`` accepts it, an exception refuses.
        self.write_answer: BaseException | None = None
        _FakeTransport.last = self

    async def _write_locked(self, data) -> None:
        if self.write_answer is not None:
            raise self.write_answer

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.link_open = False


class _FakeConnectionManager:
    """Mirrors the library's guard: it will not close a transport it never saw connect."""

    def __init__(self, connection: _FakeTransport) -> None:
        self.connection = connection
        self._is_connected = False

    async def disconnect(self) -> None:
        if self._is_connected:  # the trap: False whenever connection.connect() raised
            await self.connection.disconnect()
            self._is_connected = False


class _FakeMeshCore:
    """Stands in for the ``meshcore.MeshCore`` *class*, one instance per connect attempt.

    ``outcomes`` drives each successive ``connect()``: an exception instance is raised (with
    the link already up, as the real GATT failure leaves it), ``"none"`` returns ``None``
    (transport up, no identity reply), ``"slow"`` hangs for a caller to cancel, a
    ``("refused", exc)`` pair has the handshake's write refused with ``exc`` — swallowed, as
    meshcore's ``send`` does, leaving the handshake empty — and anything else is returned as
    a successful handshake.
    """

    #: Every instance built during a test, in order (a retry builds a second one).
    built: list[_FakeMeshCore] = []
    #: Consumed one entry per connect attempt.
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
        self.cx.link_open = True  # bleak is connected by the time anything below fails
        if isinstance(outcome, BaseException):
            # The GATT subscribe fails here — after the link is up and *before* the manager
            # records it. Nothing downstream will close it unless someone reaches the cx.
            raise outcome
        if isinstance(outcome, tuple) and outcome[0] == "refused":
            self.cx.write_answer = outcome[1]
            try:  # meshcore's send: log the failure, return False, and carry on
                await self.cx._write_locked(b"")
            except Exception:
                pass
            return None
        if outcome == "slow":
            await asyncio.sleep(30)  # a handshake the caller's wait_for will cancel
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
    """Make ``from meshcore import BLEConnection`` inside the connect path resolve to a fake."""
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
    """A stand-in for the GATT failure a PIN-protected companion answers with."""


def test_a_raising_connect_releases_the_link_it_left_open() -> None:
    """THE bug: a connect that raises must not strand an open link.

    ``create_ble`` would have swallowed the reference here. Owning the client lets the
    failure path put the link down before the exception continues on its way — so the
    peripheral stops holding a phantom peer and goes on advertising for the PIN retry.
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
    """The regression guard for the *first* attempt at this fix, which still leaked.

    ``ConnectionManager.disconnect`` is a no-op after a raising connect, because the flag it
    guards on is set only once ``connection.connect()`` has returned. Calling ``mc.disconnect``
    and stopping there looks like a teardown and closes nothing — so this asserts the graceful
    call really is inert on this path, and that the link went down anyway.
    """
    _FakeMeshCore.reset(_AuthError("Insufficient Authentication"))
    dev = _device()

    with pytest.raises(_AuthError):
        asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    client = _FakeMeshCore.built[0]
    assert client.graceful_calls == 1  # it was tried…
    assert client.connection_manager._is_connected is False  # …and the guard refused it
    assert client.cx.link_open is False  # …so the direct transport close is what saved us
    assert client.stop_calls == 1  # and the dispatcher was cancelled rather than awaited


def test_an_unanswered_handshake_closes_the_link_too() -> None:
    """Transport up, no identity reply: reported as "not a companion", link still released."""
    _FakeMeshCore.reset("none")
    dev = _device()

    assert asyncio.run(dev._connect_owned_ble(_FakeMeshCore)) is None
    assert _FakeMeshCore.built[0].cx.link_open is False


def test_a_good_connect_hands_the_client_over_still_open() -> None:
    """The success path must not close anything — the caller owns the live client."""
    _FakeMeshCore.reset("ok")
    dev = _device()

    client = asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert client is _FakeMeshCore.built[0]
    assert client.graceful_calls == 0
    assert client.cx.link_open is True


def test_the_connection_is_built_from_this_device_s_own_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Address and discovered ``BLEDevice`` reach the connection we construct; the PIN doesn't."""
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
    # auto_reconnect stays off: MeshTerm drives reconnection itself.
    assert (client.default_timeout, client.auto_reconnect) == (7.5, False)


def test_the_pin_is_withheld_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """No PIN is handed to macOS: supplying one is what *breaks* the connection there.

    ``BLEConnection.connect`` responds to a PIN by calling bleak's ``pair()``, and
    CoreBluetooth has no pairing API — the macOS backend raises outright, whereupon the
    library disconnects and re-raises. Pairing there is the OS's to run, prompted by the
    unbonded subscribe, so saying nothing is what lets a protected companion bond at all.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    _FakeMeshCore.reset("ok")
    dev = MeshCoreDevice(transport="ble", address=_ADDR, pin="000000")

    asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert _FakeTransport.last.pin is None
    assert dev._pin == "000000"  # still remembered — it is the platform that can't use it


def test_the_pin_is_withheld_from_bleak_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux MeshTerm pairs through its own BlueZ agent, so bleak never gets the PIN.

    bleak's BlueZ ``pair()`` ignores it and pairs through whatever system agent there is — a
    desktop dialog, or nothing — so handing it over would only start a second pairing
    nobody can answer.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    _FakeMeshCore.reset("ok")
    dev = MeshCoreDevice(transport="ble", address=_ADDR, pin="000000")

    asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert _FakeTransport.last.pin is None
    assert dev._pin == "000000"


def test_the_pin_is_withheld_from_bleak_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bleak's WinRT ``pair()`` is Just Works only, so a PIN handed to it bonds *without* one.

    After our own PIN pairing failed on a mistyped PIN, that fallback left Windows holding an
    unauthenticated bond, and the next attempt — right PIN — reused it and was refused.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    _FakeMeshCore.reset("ok")
    dev = MeshCoreDevice(transport="ble", address=_ADDR, pin="000000")

    asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert _FakeTransport.last.pin is None
    assert dev._pin == "000000"


def test_a_refused_handshake_write_is_a_pairing_problem() -> None:
    """A firmware that guards only the write (MeshOS, wadamesh on a T-Deck) is still paired.

    The subscribe succeeds there, and the refusal lands on the handshake's first write, which
    meshcore's ``send`` swallows. The empty handshake must surface as that refusal — not as
    "not a companion" — so the pairing, the stale-bond repair and the macOS wait all run.
    """
    refusal = _AuthError("GATT Protocol Error: Insufficient Encryption")
    _FakeMeshCore.reset(("refused", refusal))
    dev = _device()

    with pytest.raises(_AuthError) as caught:
        asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert caught.value is refusal
    assert _FakeMeshCore.built[0].cx.link_open is False  # released before it was raised


def test_a_write_failing_for_another_reason_still_reads_as_no_answer() -> None:
    """Only a refusal for want of a bond is promoted; a dropped write stays "no answer"."""
    _FakeMeshCore.reset(("refused", OSError("device unreachable")))
    dev = _device()

    assert asyncio.run(dev._connect_owned_ble(_FakeMeshCore)) is None


def test_a_refused_write_heals_through_the_pin_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end on Windows: refused write → unpair and pair with the PIN → connected.

    The T-Deck case exactly: a Just Works bond is trusted on the fast path, the handshake's
    write is refused over it, and the repair that stock firmware reaches from the subscribe
    must be reached from the write too.
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
    """A probe's ``wait_for`` expiring mid-handshake must not leak what it cancelled.

    This is the second way in, and the reason the teardown is shielded: a plain ``await``
    would itself be cancelled the moment it suspended, abandoning the close.
    """
    _FakeMeshCore.reset("slow")
    dev = _device()

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(dev._connect_owned_ble(_FakeMeshCore), timeout=0.1)
        # The shielded close runs on after the cancellation propagates; give it a tick.
        await asyncio.sleep(0.2)
        return _FakeMeshCore.built[0]

    client = asyncio.run(scenario())
    assert client.cx.link_open is False, "a cancelled handshake stranded its link"


def test_a_failing_teardown_never_masks_the_real_error() -> None:
    """Teardown is best-effort: the connect's own failure is what the caller must see."""

    class _Stubborn(_FakeMeshCore):
        async def disconnect(self):
            self.graceful_calls += 1
            raise RuntimeError("teardown exploded")

    _Stubborn.reset(_AuthError("Insufficient Authentication"))
    dev = _device()

    with pytest.raises(_AuthError):
        asyncio.run(dev._connect_owned_ble(_Stubborn))
    # The graceful half blew up, and the forced close still ran and did the job.
    assert _Stubborn.built[0].cx.link_open is False


def test_a_wedged_teardown_is_not_waited_out_forever() -> None:
    """A dispatcher stop that deadlocks is abandoned, and the transport closed regardless."""

    class _Wedged(_FakeMeshCore):
        async def disconnect(self):
            self.graceful_calls += 1
            await asyncio.sleep(30)  # the library's queue.join() deadlock

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
    """A retried link-open builds a fresh client, and the abandoned one was released."""
    _FakeMeshCore.reset(ConnectionError("Failed to connect to device"), "ok")
    dev = _device()

    client = asyncio.run(dev._create_ble_with_retry(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == 2, "the retry did not build its own client"
    assert _FakeMeshCore.built[0].cx.link_open is False, "the failed attempt leaked"
    assert client is _FakeMeshCore.built[1]
    assert client.cx.link_open is True


def test_every_link_open_attempt_failing_raises_the_last_error() -> None:
    """Exhausting the retries says the link never opened — with nothing left open."""
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
    """Stand-in for ``bleak.exc.BleakError``, which carries what happened only as text."""


def test_a_link_that_drops_while_connecting_is_retried() -> None:
    """BlueZ losing the peer during service discovery is the radio, so it gets the retry.

    Seen on a Cardputer Zero: an HCI Connection Timeout 1.7 s in, with no security
    exchange begun, and the next attempt connected.
    """
    _FakeMeshCore.reset(_BleakError("failed to discover services, device disconnected"), "ok")
    dev = _device()

    client = asyncio.run(dev._create_ble_with_retry(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == 2
    assert _FakeMeshCore.built[0].cx.link_open is False, "the dropped attempt leaked"
    assert client is _FakeMeshCore.built[1]


def test_an_auth_refusal_that_ends_in_a_disconnect_is_never_retried() -> None:
    """A refused PIN can drop the link too; it goes to the PIN handling, not round again."""
    _FakeMeshCore.reset(_AuthError("Insufficient Authentication: device disconnected"), "ok")
    dev = _device()

    with pytest.raises(_AuthError):
        asyncio.run(dev._create_ble_with_retry(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == 1


def test_any_other_failure_is_not_retried() -> None:
    """Only a link that never opened or that dropped is the radio's; the rest surface at once."""
    _FakeMeshCore.reset(_BleakError("Characteristic 6e400003 was not found"), "ok")
    dev = _device()

    with pytest.raises(_BleakError):
        asyncio.run(dev._create_ble_with_retry(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == 1


class _Connection:
    """A ``BLEConnection`` stand-in: just the drop handler the guard wraps."""

    def __init__(self) -> None:
        self.forwarded: list[object] = []

    def handle_disconnect(self, client: object) -> None:
        self.forwarded.append(client)


def test_a_hang_up_after_the_pin_was_asked_is_flagged_not_held() -> None:
    """The retries bleak makes are held back; a companion refusing its pairing is not one."""
    asked = False
    connection = _Connection()
    seen = conn_mod._hold_disconnects_while_connecting(connection, lambda: asked)

    connection.handle_disconnect("first try")  # bleak's 0x3e retry: held, nothing flagged
    assert not seen.hung_up.is_set()
    asked = True
    connection.handle_disconnect("after the PIN")
    assert seen.hung_up.is_set()
    assert connection.forwarded == []  # still held while connecting


def test_a_hang_up_ends_the_connect_as_a_pairing_failure() -> None:
    """The connect stops waiting on a dead link and says why, in the PIN handling's words."""

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
    """No hang-up, no change: the connect's own answer comes back."""

    async def scenario() -> str:
        async def connect() -> str:
            return "started"

        return await conn_mod._unless_hung_up(connect(), asyncio.Event())

    assert asyncio.run(scenario()) == "started"
