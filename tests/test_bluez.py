# SPDX-License-Identifier: Apache-2.0
"""Tests for MeshTerm's own BlueZ pairing (Linux PIN pairing through a D-Bus agent).

bleak's BlueZ ``pair()`` never delivers a PIN — BlueZ asks a pairing *agent* for it and bleak
registers none — so a PIN-protected companion could not bond on Linux at all.
:mod:`meshterm.core.bluez` registers an agent for the length of one pairing. These tests run
against a scripted stand-in for the system bus: no BlueZ, no radio, and no real address or PIN.
``dbus_fast`` is bleak's Linux dependency; its ``Message`` type is all that is needed here.
"""

from __future__ import annotations

import asyncio
import os

import pytest

dbus_fast = pytest.importorskip("dbus_fast")
from dbus_fast import Message, MessageType  # noqa: E402

from meshterm.core import bluez  # noqa: E402

_ADDR = "00:11:22:33:44:55"
_DEV = "/org/bluez/hci0/dev_00_11_22_33_44_55"
_PIN = "123456"


def _agent_call(member: str, *body, path: str = "/agent") -> Message:
    """A method call BlueZ would send the agent."""
    signature = {
        "RequestPasskey": "o",
        "RequestPinCode": "o",
        "RequestConfirmation": "ou",
        "RequestAuthorization": "o",
        "AuthorizeService": "os",
        "Cancel": "",
    }[member]
    return Message(
        path=path,
        interface="org.bluez.Agent1",
        member=member,
        signature=signature,
        body=list(body),
        serial=1,
    )


def test_status_names_read_as_words() -> None:
    """A BlueZ error name becomes the words the connection's status sets use."""
    assert bluez.status_name("org.bluez.Error.AuthenticationFailed") == "authentication failed"
    assert bluez.status_name("org.bluez.Error.ConnectionAttemptFailed") == (
        "connection attempt failed"
    )
    # The bare Failed says nothing, so BlueZ's own detail rides along.
    assert bluez.status_name("org.bluez.Error.Failed", ["le-connection-abort-by-local"]) == (
        "failed: le-connection-abort-by-local"
    )


def test_is_mac() -> None:
    """Only a 48-bit address can be paired through BlueZ (a macOS UUID cannot)."""
    assert bluez.is_mac(_ADDR) and bluez.is_mac("aa-bb-cc-dd-ee-ff")
    assert not bluez.is_mac("550e8400-e29b-41d4-a716-446655440000")
    assert not bluez.is_mac("")


def test_agent_answers_the_passkey_with_the_pin() -> None:
    """RequestPasskey is answered with the PIN as a number, and the agent notes it was asked."""
    agent = bluez.PinAgent("/agent", _PIN, _DEV)
    reply = agent.handle(_agent_call("RequestPasskey", _DEV))
    assert reply.message_type == MessageType.METHOD_RETURN
    assert reply.signature == "u" and reply.body == [123456]
    assert agent.asked


def test_agent_refuses_another_device_and_other_paths() -> None:
    """A pairing MeshTerm didn't start is never approved, and foreign calls are left alone."""
    agent = bluez.PinAgent("/agent", _PIN, _DEV)
    reply = agent.handle(_agent_call("RequestPasskey", "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"))
    assert reply.message_type == MessageType.ERROR
    assert reply.error_name == "org.bluez.Error.Rejected"
    assert not agent.asked
    # Not addressed to the agent's path: none of its business.
    assert agent.handle(_agent_call("RequestPasskey", _DEV, path="/elsewhere")) is None


def test_agent_confirms_only_the_number_its_pin_names() -> None:
    """Numeric Comparison is approved only for our own PIN's number."""
    agent = bluez.PinAgent("/agent", _PIN, _DEV)
    ok = agent.handle(_agent_call("RequestConfirmation", _DEV, 123456))
    assert ok.message_type == MessageType.METHOD_RETURN
    wrong = agent.handle(_agent_call("RequestConfirmation", _DEV, 654321))
    assert wrong.message_type == MessageType.ERROR


class _Reply:
    """A reply as ``bus.call`` returns it."""

    def __init__(self, body=None, error: str | None = None, detail: str = "") -> None:
        self.message_type = MessageType.ERROR if error else MessageType.METHOD_RETURN
        self.error_name = error
        self.body = [detail] if error else (body or [])


class _Variant:
    def __init__(self, value) -> None:
        self.value = value


class _FakeBus:
    """A scripted system bus: BlueZ's object tree plus the answer to ``Pair``.

    ``pair_reply`` is what ``Device1.Pair`` returns — a list is answered one entry per
    call, to script a retry; ``asks`` makes BlueZ call the agent's ``RequestPasskey``
    first, as a real Passkey Entry pairing does.
    """

    def __init__(self, *, known=True, paired=False, pair_reply=None, asks=True) -> None:
        self.known, self.paired = known, paired
        self.pair_reply = pair_reply or _Reply()
        self.asks = asks
        self.calls: list[str] = []
        self.handlers: list = []
        self.agent_answer = None
        self.disconnected = False

    def _objects(self) -> dict:
        objects = {"/org/bluez/hci0": {"org.bluez.Adapter1": {}}}
        if self.known:
            objects[_DEV] = {
                "org.bluez.Device1": {
                    "Address": _Variant(_ADDR),
                    "Paired": _Variant(self.paired),
                    "Adapter": _Variant("/org/bluez/hci0"),
                }
            }
        return objects

    async def call(self, msg):
        self.calls.append(msg.member)
        if msg.member == "GetManagedObjects":
            return _Reply([self._objects()])
        if msg.member == "RemoveDevice":
            self.known = self.paired = False
            return _Reply()
        if msg.member == "StartDiscovery":
            self.known = True  # the device is heard again
            return _Reply()
        if msg.member == "Pair":
            if self.asks:
                for handler in self.handlers:
                    self.agent_answer = handler(_agent_call("RequestPasskey", _DEV, path=_AGENT))
            if isinstance(self.pair_reply, list):
                return self.pair_reply.pop(0)
            return self.pair_reply
        return _Reply()

    def add_message_handler(self, handler) -> None:
        self.handlers.append(handler)

    def remove_message_handler(self, handler) -> None:
        self.handlers.remove(handler)

    def disconnect(self) -> None:
        self.disconnected = True


_AGENT = f"/net/meshterm/agent{os.getpid()}"


@pytest.fixture
def bus(monkeypatch):
    """Point :mod:`bluez` at a fresh fake bus."""
    holder: dict = {}

    async def _system_bus():
        return holder["bus"]

    monkeypatch.setattr(bluez, "_system_bus", _system_bus)

    def _make(**kwargs) -> _FakeBus:
        holder["bus"] = _FakeBus(**kwargs)
        return holder["bus"]

    return _make


def test_pair_registers_an_agent_answers_the_pin_and_trusts(bus) -> None:
    """The happy path: agent in, Pair, PIN answered, Trusted set, agent out, bus closed."""
    fake = bus()
    assert asyncio.run(bluez.pair(_ADDR, _PIN, force=False, discover_s=0)) == ("paired", "")
    # Disconnect: Pair leaves its link up, and a connected companion stops advertising.
    assert fake.calls[1:] == ["RegisterAgent", "Pair", "Set", "Disconnect", "UnregisterAgent"]
    assert fake.agent_answer.body == [123456]  # the PIN reached BlueZ as the passkey
    assert fake.handlers == [] and fake.disconnected


def test_pair_trusts_an_existing_bond_unless_forced(bus) -> None:
    """An existing bond is reused; ``force`` removes it and pairs afresh."""
    fake = bus(paired=True)
    assert asyncio.run(bluez.pair(_ADDR, _PIN, force=False, discover_s=0)) == ("reused", "")
    assert "Pair" not in fake.calls

    fake = bus(paired=True)
    assert asyncio.run(bluez.pair(_ADDR, _PIN, force=True, discover_s=1)) == ("paired", "")
    assert fake.calls.index("RemoveDevice") < fake.calls.index("Pair")


def test_pair_reports_a_wrong_pin_and_a_refusal_apart(bus) -> None:
    """AuthenticationFailed after the PIN was asked is a wrong PIN; before it, a refusal."""
    bus(pair_reply=_Reply(error="org.bluez.Error.AuthenticationFailed"))
    assert asyncio.run(bluez.pair(_ADDR, _PIN, force=False, discover_s=0)) == (
        "failed",
        "authentication failed",
    )
    bus(pair_reply=_Reply(error="org.bluez.Error.AuthenticationFailed"), asks=False)
    assert asyncio.run(bluez.pair(_ADDR, _PIN, force=False, discover_s=0)) == (
        "failed",
        "authentication rejected",
    )


def test_pair_listens_for_a_forgotten_device_then_gives_up(bus) -> None:
    """A device BlueZ has forgotten is listened for; one never heard is ``absent``."""
    fake = bus(known=False)
    assert asyncio.run(bluez.pair(_ADDR, _PIN, force=False, discover_s=1))[0] == "paired"
    assert fake.calls.count("StartDiscovery") == fake.calls.count("StopDiscovery") == 1


def test_pair_without_a_bus_is_an_error_not_a_raise(monkeypatch) -> None:
    """No system bus (a container, no BlueZ) reports ``error`` rather than raising."""

    async def _no_bus():
        raise OSError("no system bus")

    monkeypatch.setattr(bluez, "_system_bus", _no_bus)
    outcome, status = asyncio.run(bluez.pair(_ADDR, _PIN, force=False))
    assert outcome == "error" and "no system bus" in status


def test_a_pin_that_is_not_digits_never_reaches_bluez(bus) -> None:
    """A passkey is a number; anything else is a wrong PIN without asking the radio."""
    fake = bus()
    assert asyncio.run(bluez.pair(_ADDR, "12ab56", force=False))[0] == "failed"
    assert fake.calls == []


def test_pair_retries_a_link_that_failed_to_come_up(bus, monkeypatch) -> None:
    """ConnectionAttemptFailed is the radio, not the pairing: Pair again, a bounded few times.

    bleak retries these inside its own connect; BlueZ's Pair does not, and on a uConsole the
    first Pair failed this way, sending the connect on unpaired into a 32 s stall.
    """
    monkeypatch.setattr(bluez, "PAIR_RETRY_DELAY_S", 0)
    flake = _Reply(error="org.bluez.Error.ConnectionAttemptFailed")
    fake = bus(pair_reply=[flake, flake, _Reply()])
    assert asyncio.run(bluez.pair(_ADDR, _PIN, force=False, discover_s=0)) == ("paired", "")
    assert fake.calls.count("Pair") == 3

    fake = bus(pair_reply=[flake] * bluez.PAIR_ATTEMPTS)
    outcome, status = asyncio.run(bluez.pair(_ADDR, _PIN, force=False, discover_s=0))
    assert (outcome, status) == ("failed", "connection attempt failed")
    assert fake.calls.count("Pair") == bluez.PAIR_ATTEMPTS  # bounded, never endless


def test_pair_never_retries_a_wrong_pin(bus, monkeypatch) -> None:
    """Only a link failure is retried; a refused passkey is an answer."""
    monkeypatch.setattr(bluez, "PAIR_RETRY_DELAY_S", 0)
    fake = bus(pair_reply=[_Reply(error="org.bluez.Error.AuthenticationFailed"), _Reply()])
    assert asyncio.run(bluez.pair(_ADDR, _PIN, force=False, discover_s=0))[0] == "failed"
    assert fake.calls.count("Pair") == 1


def test_agent_without_a_pin_refuses_at_once() -> None:
    """No PIN: every passkey question is refused immediately rather than left unanswered."""
    agent = bluez.PinAgent("/agent", None, bluez.device_tail(_ADDR))
    for member, body in (("RequestPasskey", [_DEV]), ("RequestConfirmation", [_DEV, 0])):
        reply = agent.handle(_agent_call(member, *body))
        assert reply.message_type == MessageType.ERROR, member
        assert reply.error_name == "org.bluez.Error.Rejected"


def test_agent_never_approves_a_pairing_without_the_passkey() -> None:
    """A "Just Works" yes/no is refused even with a PIN: that is how a bond ends up weak."""
    agent = bluez.PinAgent("/agent", _PIN, _DEV)
    reply = agent.handle(_agent_call("RequestAuthorization", _DEV))
    assert reply.message_type == MessageType.ERROR


def test_agent_matches_a_device_by_its_path_tail() -> None:
    """Before a connect the adapter is unknown, so the agent matches the ``/dev_…`` tail."""
    agent = bluez.PinAgent("/agent", _PIN, bluez.device_tail("00-11-22-33-44-55"))
    assert agent.handle(_agent_call("RequestPasskey", _DEV)).body == [123456]
    reply = agent.handle(_agent_call("RequestPasskey", "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"))
    assert reply.message_type == MessageType.ERROR


def test_answering_is_the_default_agent_for_the_connect_and_then_steps_down(bus) -> None:
    """Registered, made default, then unregistered with the bus closed — in that order."""
    fake = bus()

    async def scenario() -> list[str]:
        async with bluez.answering(_ADDR, None):
            during = list(fake.calls)
        return during

    during = asyncio.run(scenario())
    assert during == ["RegisterAgent", "RequestDefaultAgent"]
    assert fake.calls[-1] == "UnregisterAgent"
    assert fake.handlers == [] and fake.disconnected


def test_answering_without_a_bus_still_runs_the_connect(monkeypatch) -> None:
    """No system bus: the connect goes ahead with no agent, and nothing raises."""

    async def _no_bus():
        raise OSError("no system bus")

    monkeypatch.setattr(bluez, "_system_bus", _no_bus)

    async def scenario() -> str:
        async with bluez.answering(_ADDR, _PIN):
            return "connected"

    assert asyncio.run(scenario()) == "connected"
