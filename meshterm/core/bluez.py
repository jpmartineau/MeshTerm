# SPDX-License-Identifier: Apache-2.0
"""Pair a Bluetooth companion with its PIN through BlueZ, the Linux Bluetooth stack.

A MeshCore companion puts its UART characteristic behind an authenticated bond (ENC+MITM).
On a device with a fixed PIN, this means Passkey Entry: the computer must type the six
digits. On Linux, the ``pair()`` of bleak cannot do that. It calls the ``Device1.Pair`` of
BlueZ and nothing else. BlueZ gets a passkey only from a **pairing agent**: an object that a
program registers on the system bus to answer ``RequestPasskey``.

On a desktop, that agent is the Settings app. In a terminal, it is ``bluetoothctl``. Over
SSH, there is usually no agent. When BlueZ has no agent to ask, it uses "Just Works"
instead. Then the firmware refuses a bond without MITM, and the PIN that MeshTerm got never
gets to the companion at all. This is the Linux equivalent of the Windows problem that
:meth:`~meshterm.core.connection.MeshCoreDevice._pair_ble_windows` solves.

Thus MeshTerm registers its own agent for the duration of one pairing. The agent answers
with the PIN that MeshTerm got, and MeshTerm calls ``Pair`` from the same bus connection.
BlueZ sends the agent requests of a pairing to the agent of the caller of ``Pair`` (the
default agent of the system is only the fallback). Thus this agent never replaces the agent
of the desktop, and never answers for the pairing of a different device.

The agent answers raw D-Bus messages (:class:`PinAgent`). It does not use the annotated
``ServiceInterface`` of ``dbus_fast``, because that class reads D-Bus signatures from
annotations such as ``device: "o"``. A linter reads such an annotation as an undefined name,
and postponed annotations change it into a quoted string. A message handler has neither
problem, and a test can exercise it without a bus.

``dbus_fast`` is a dependency of bleak on Linux, so MeshTerm installs nothing new. This
module imports it lazily. Each entry point here is best-effort: if the system bus or BlueZ
is missing, the entry point reports an ``"error"`` outcome and does not raise.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

_log = logging.getLogger(__name__)

_BLUEZ = "org.bluez"
_AGENT_IFACE = "org.bluez.Agent1"
_AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
_ADAPTER_IFACE = "org.bluez.Adapter1"
_DEVICE_IFACE = "org.bluez.Device1"
_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
_OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
_REJECTED = "org.bluez.Error.Rejected"
_ALREADY_EXISTS = "org.bluez.Error.AlreadyExists"
_AGENTS = "/org/bluez"  # the object path of AgentManager1

#: The input and output capability that the agent declares. The companion is the side with
#: the PIN (``DisplayOnly``), so we are the keyboard. ``KeyboardOnly`` makes the pairing
#: Passkey Entry, and we type the passkey. This is the only method that a fixed PIN can
#: satisfy.
AGENT_CAPABILITY = "KeyboardOnly"

#: How long discovery runs for a device that BlueZ has forgotten (seconds). BlueZ removes an
#: unpaired device approximately half a minute after it stops hearing that device. Thus a
#: device that the picker showed some time ago may have to be heard again before it can be
#: paired.
DISCOVER_S = 6.0

#: The time limit on the ``Pair`` call itself (seconds). A healthy passkey pairing takes one
#: to three seconds. This limit must only be longer than a slow Bluetooth link, and it stays
#: well in the time window of the probe.
PAIR_TIMEOUT_S = 15.0

#: The number of ``Pair`` attempts when the link under it does not come up, and the pause
#: between them. On the Bluetooth hardware of a Raspberry Pi class machine, three to seven
#: link attempts fail for each connect (``le-connection-abort-by-local``, HCI 0x3e). bleak
#: tries these again in its own ``connect()``. The ``Pair`` of BlueZ does not, and it
#: answers ConnectionAttemptFailed immediately. Measured on a uConsole: the first Pair
#: failed in that way, then the connect continued without a pairing, and the protected
#: subscribe cost 32 s before the recovery paired it after all.
PAIR_ATTEMPTS = 4
PAIR_RETRY_DELAY_S = 0.5

_LINK_FLAKES = ("org.bluez.Error.ConnectionAttemptFailed",)


def _link_flake(reply: Any) -> bool:
    """Whether a ``Pair`` error is a link that did not come up, which is worth one more try."""
    detail = reply.body[0] if reply.body and isinstance(reply.body[0], str) else ""
    return reply.error_name in _LINK_FLAKES or "le-connection-abort" in detail


_MAC = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


def is_mac(address: str) -> bool:
    """Whether ``address`` is a 48-bit Bluetooth address, separated by colons or dashes."""
    return bool(_MAC.match(address or ""))


def _normal(address: str) -> str:
    """An address in the form that BlueZ uses: upper case, separated by colons."""
    return address.replace("-", ":").upper()


def status_name(error_name: str, body: list[Any] | None = None) -> str:
    """Change a BlueZ error into the lowercase words that a message can quote.

    ``org.bluez.Error.AuthenticationFailed`` → ``"authentication failed"``. The bare
    ``org.bluez.Error.Failed`` gives no information alone, so its text goes with it
    (``"failed: le-connection-abort-by-local"``).

    Args:
        error_name: The D-Bus error name from the reply.
        body: The body of the error reply. Its first item is the detail string of BlueZ.

    Returns:
        The status, in the form that :data:`~meshterm.core.connection._PAIRING_REFUSED`
        and its related sets use.
    """
    leaf = error_name.rsplit(".", 1)[-1]
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", leaf).lower()
    detail = body[0] if body and isinstance(body[0], str) else ""
    if words == "failed" and detail:
        return f"failed: {detail}"
    return words


class PinAgent:
    """A BlueZ ``Agent1`` that answers the pairing of one device with one PIN, or refuses it.

    MeshTerm installs it as a message handler on the bus that calls ``Pair`` (refer to the
    module docstring for the reason for that bus). Or MeshTerm makes it the default agent of
    BlueZ for the duration of a connect (refer to :func:`answering`). It answers only calls
    to its own object path, and only for the device for which it was built. It answers each
    other call with ``Rejected``, so it can never approve a pairing that we did not start.
    Without a PIN, it refuses each request immediately. In that case, this is its purpose:
    the alternative is that BlueZ waits for an agent that does not exist.

    It never approves a pairing without the passkey (``RequestAuthorization``, the yes/no
    of "Just Works"). That is how a companion gets an unauthenticated bond: the encryption
    works, but the UART characteristic still refuses. This is the stale-bond failure itself.

    Attributes:
        path: The object path at which the agent is registered.
        asked: Whether BlueZ asked for the PIN. If a pairing failed and BlueZ did not ask,
            the pairing was refused before the passkey stage, and that is a different
            problem.
    """

    def __init__(self, path: str, pin: str | None, device_path: str) -> None:
        """Build the agent.

        Args:
            path: The object path at which the agent answers.
            pin: The pairing PIN (digits), or ``None`` to refuse each request.
            device_path: The only device for which the agent can answer: its BlueZ object
                path, or the ``/dev_…`` end of that path when the adapter is not known yet
                (refer to :func:`device_tail`).
        """
        self.path = path
        self._pin = pin
        self._device = device_path
        self.asked = False

    def handle(self, msg: Any) -> Any:
        """Answer one incoming message, or return ``None`` to leave it to other handlers.

        Args:
            msg: A ``dbus_fast.Message``.

        Returns:
            A reply ``Message``, or ``None`` when the message is not for this agent.
        """
        from dbus_fast import Message, MessageType

        if (
            msg.message_type != MessageType.METHOD_CALL
            or msg.path != self.path
            or msg.interface != _AGENT_IFACE
        ):
            return None
        member = msg.member
        if member in ("Release", "Cancel"):
            return Message.new_method_return(msg)
        device = msg.body[0] if msg.body else ""
        if not str(device).endswith(self._device):
            return Message.new_error(msg, _REJECTED, "not the device MeshTerm is pairing")
        if member in ("RequestPasskey", "RequestPinCode", "RequestConfirmation") and not self._pin:
            self.asked = True
            return Message.new_error(msg, _REJECTED, "no PIN was given for this device")
        if member == "RequestPasskey":
            self.asked = True
            return Message.new_method_return(msg, "u", [int(self._pin)])
        if member == "RequestPinCode":
            self.asked = True
            return Message.new_method_return(msg, "s", [self._pin])
        if member == "RequestConfirmation":
            # Numeric Comparison: approve only the number that our PIN gives. A companion
            # with a fixed PIN never asks for this. If a companion asks and shows a
            # different number, it is not the pairing that MeshTerm was asked to make.
            self.asked = True
            if int(msg.body[1]) == int(self._pin):
                return Message.new_method_return(msg)
            return Message.new_error(msg, _REJECTED, "passkey does not match the PIN")
        if member in ("DisplayPasskey", "DisplayPinCode"):
            return Message.new_method_return(msg)
        return Message.new_error(msg, _REJECTED, f"{member} is not something MeshTerm answers")


def device_tail(address: str) -> str:
    """The ``/dev_AA_BB_…`` end of the object path that BlueZ gives the device at ``address``."""
    return "/dev_" + _normal(address).replace(":", "_")


@asynccontextmanager
async def answering(address: str, pin: str | None) -> AsyncIterator[PinAgent]:
    """Be the default pairing agent of BlueZ for one device while a connect runs.

    BlueZ does not wait for a request to pair. When the companion refuses the UART
    subscribe with *Insufficient Authentication*, BlueZ itself raises the security of the
    link and starts SMP pairing. This pairing must have an agent, and BlueZ asks the
    **default** agent, not ours (that routing is only for a ``Pair`` that we call). If no
    agent is registered, as on each headless machine, BlueZ offers ``DisplayYesNo`` to the
    companion and gets "Just Works". Then it asks a yes/no question that nobody is there to
    answer, and the companion disconnects at its 30 s SMP timeout.

    Measured on a uConsole with btmon: 3.3 s to the question, 33.5 s to the disconnect, and
    only then "requires a PIN". On a desktop, the question goes to the dialog of the
    desktop instead. If the user answers it, the result is the unauthenticated bond that
    later refuses the subscribe.

    Thus, for the duration of the connect, MeshTerm is the default agent, for this device
    only. It declares ``KeyboardOnly``, so the pairing that BlueZ starts is Passkey Entry.
    With a PIN, the agent types the PIN, and the connect pairs immediately. Without a PIN,
    the agent refuses immediately, and the refusal arrives in seconds. If a different
    device tries to pair in that time window, the agent refuses it, and does not let it
    wait.

    BlueZ keeps default agents as a stack. Thus, when MeshTerm unregisters its agent, the
    role goes back to the agent that had it before. This is best-effort: if there is no
    system bus, this function does nothing.

    Args:
        address: The Bluetooth address of the companion.
        pin: Its PIN, or ``None`` to refuse.

    Yields:
        The agent, live for the body of the ``async with``. Its ``asked`` tells whether
        BlueZ asked it for the PIN. With this, the connect can tell the difference between
        a companion that disconnected during a pairing and a link that was only lost.
    """
    bus = None
    agent = PinAgent(f"/net/meshterm/connect{os.getpid()}", pin, device_tail(address))
    registered = False
    try:
        bus = await _system_bus()
        bus.add_message_handler(agent.handle)
        reply = await _call(
            bus, _AGENTS, _AGENT_MANAGER_IFACE, "RegisterAgent", "os",
            [agent.path, AGENT_CAPABILITY],
        )  # fmt: skip
        registered = not _is_error(reply)
        if registered:
            await _call(
                bus, _AGENTS, _AGENT_MANAGER_IFACE, "RequestDefaultAgent", "o", [agent.path]
            )
    except Exception as exc:  # noqa: BLE001 - no bus, no BlueZ: connect without an agent
        _log.debug("couldn't stand in as the BlueZ agent: %s", exc)
    try:
        yield agent
    finally:
        if bus is not None:
            try:
                if registered:
                    await _call(
                        bus, _AGENTS, _AGENT_MANAGER_IFACE, "UnregisterAgent", "o", [agent.path]
                    )
            except Exception as exc:  # noqa: BLE001 - the bus closing drops it anyway
                _log.debug("unregistering the BlueZ agent failed: %s", exc)
            bus.remove_message_handler(agent.handle)
            bus.disconnect()


async def _call(bus: Any, path: str, interface: str, member: str, signature: str = "", body=None):
    """Call a BlueZ method and return the raw reply (the caller handles the errors)."""
    from dbus_fast import Message

    return await bus.call(
        Message(
            destination=_BLUEZ,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body or [],
        )
    )


def _is_error(reply: Any) -> bool:
    from dbus_fast import MessageType

    return reply.message_type == MessageType.ERROR


async def _objects(bus: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """All the BlueZ objects, from ``GetManagedObjects`` (empty when BlueZ did not answer)."""
    reply = await _call(bus, "/", _OBJECT_MANAGER_IFACE, "GetManagedObjects")
    if _is_error(reply) or not reply.body:
        return {}
    return reply.body[0]


def _value(props: dict[str, Any], name: str) -> Any:
    """The plain value of a property, without the ``Variant`` that BlueZ sends."""
    raw = props.get(name)
    return getattr(raw, "value", raw)


def _device_in(objects: dict[str, dict[str, dict[str, Any]]], address: str) -> str | None:
    """The object path of the device at ``address``, if BlueZ knows it."""
    want = _normal(address)
    for path, interfaces in objects.items():
        props = interfaces.get(_DEVICE_IFACE)
        if props is not None and str(_value(props, "Address") or "").upper() == want:
            return path
    return None


def _adapter_in(objects: dict[str, dict[str, dict[str, Any]]]) -> str | None:
    """The object path of the first Bluetooth adapter."""
    return next((path for path, ifaces in objects.items() if _ADAPTER_IFACE in ifaces), None)


async def _find_device(bus: Any, address: str, discover_s: float) -> tuple[str | None, bool]:
    """Find the object path of the device. If BlueZ forgot it, listen for it a short time.

    Returns:
        ``(path, paired)``. ``path`` is ``None`` when the device could not be heard.
    """
    objects = await _objects(bus)
    path = _device_in(objects, address)
    if path is None:
        adapter = _adapter_in(objects)
        if adapter is None:
            return None, False
        # A refusal here is not a problem: bleak or the desktop may already run discovery,
        # and that is all that we need.
        await _call(bus, adapter, _ADAPTER_IFACE, "StartDiscovery")
        try:
            deadline = asyncio.get_running_loop().time() + discover_s
            while path is None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.5)
                objects = await _objects(bus)
                path = _device_in(objects, address)
        finally:
            await _call(bus, adapter, _ADAPTER_IFACE, "StopDiscovery")
    if path is None:
        return None, False
    return path, bool(_value(objects[path][_DEVICE_IFACE], "Paired"))


def _adapter_of(objects: dict[str, dict[str, dict[str, Any]]], path: str) -> str | None:
    """The adapter of a device: its own ``Adapter`` property, or else the first adapter."""
    own = _value(objects.get(path, {}).get(_DEVICE_IFACE, {}), "Adapter")
    return str(own) if own else _adapter_in(objects)


async def _remove(bus: Any, path: str) -> Any:
    """Forget the bond of a device (``Adapter1.RemoveDevice``), and also the device itself.

    Returns:
        The raw reply, or ``None`` when there was no adapter to ask.
    """
    adapter = _adapter_of(await _objects(bus), path)
    if adapter is None:
        return None
    return await _call(bus, adapter, _ADAPTER_IFACE, "RemoveDevice", "o", [path])


async def _system_bus() -> Any:
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    return await MessageBus(bus_type=BusType.SYSTEM).connect()


async def pair(
    address: str,
    pin: str,
    *,
    force: bool,
    discover_s: float = DISCOVER_S,
    pair_timeout_s: float = PAIR_TIMEOUT_S,
) -> tuple[str, str]:
    """Pair the companion at ``address`` with ``pin``, through our own BlueZ agent.

    This function has the same contract as the Windows procedure. It trusts an existing
    bond, unless ``force`` is set. ``force`` removes the bond and pairs again: this is the
    recovery for a bond that the device lost after the pairing.

    Args:
        address: The Bluetooth address of the companion.
        pin: Its pairing PIN (six digits).
        force: Whether to remove an existing bond first.
        discover_s: How long to listen for a device that BlueZ has forgotten.
        pair_timeout_s: The time limit on the ``Pair`` call.

    Returns:
        ``(outcome, status)``, in the vocabulary of
        :class:`~meshterm.core.connection._BlePairing`: ``"reused"``, ``"paired"``,
        ``"absent"``, ``"failed"`` with the BlueZ status, or ``"error"`` with a description
        of the problem.
    """
    if not pin.isdigit():
        return "failed", "authentication failed"  # a passkey is a number: this is not one
    try:
        bus = await _system_bus()
    except Exception as exc:  # noqa: BLE001 - no system bus / no dbus_fast: nothing to pair with
        return "error", f"couldn't reach BlueZ: {exc}"
    try:
        path, paired = await _find_device(bus, address, discover_s)
        if path is None:
            return "absent", ""
        if paired:
            if not force:
                return "reused", ""
            await _remove(bus, path)
            path, _ = await _find_device(bus, address, discover_s)
            if path is None:
                return "absent", ""
        agent = PinAgent(f"/net/meshterm/agent{os.getpid()}", pin, path)
        paired_link: str | None = None  # set when MeshTerm asks Pair to open a link
        bus.add_message_handler(agent.handle)
        try:
            reply = await _call(
                bus, _AGENTS, _AGENT_MANAGER_IFACE, "RegisterAgent", "os",
                [agent.path, AGENT_CAPABILITY],
            )  # fmt: skip
            if _is_error(reply):
                return "error", status_name(reply.error_name, reply.body)
            paired_link = path
            for attempt in range(PAIR_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(PAIR_RETRY_DELAY_S)
                try:
                    reply = await asyncio.wait_for(
                        _call(bus, path, _DEVICE_IFACE, "Pair"), pair_timeout_s
                    )
                except asyncio.TimeoutError:
                    await _call(bus, path, _DEVICE_IFACE, "CancelPairing")
                    return "failed", "authentication timeout"
                if not (_is_error(reply) and _link_flake(reply)):
                    break
                _log.debug("BlueZ Pair lost its link (attempt %d/%d)", attempt + 1, PAIR_ATTEMPTS)
            if _is_error(reply) and reply.error_name != _ALREADY_EXISTS:
                status = status_name(reply.error_name, reply.body)
                if not agent.asked and status == "authentication failed":
                    # Refused before the passkey stage: the PIN was not the problem.
                    status = "authentication rejected"
                return "failed", status
            # Trusted lets later connections through, with no agent to authorize them.
            from dbus_fast import Variant

            await _call(
                bus, path, _PROPERTIES_IFACE, "Set", "ssv",
                [_DEVICE_IFACE, "Trusted", Variant("b", True)],
            )  # fmt: skip
            return "paired", ""
        finally:
            if paired_link is not None:
                # ``Pair`` opens a link for the pairing, and BlueZ keeps the link up. A
                # connected companion stops its BLE advertisements. The connect that
                # follows finds the device with a scan, so it never finds the device.
                # Measured on a uConsole: paired, then "couldn't open a link" after two
                # tries of 30 s. Close the link, so that the connect finds a device that
                # advertises, the same as without this pairing.
                await _call(bus, paired_link, _DEVICE_IFACE, "Disconnect")
            await _call(bus, _AGENTS, _AGENT_MANAGER_IFACE, "UnregisterAgent", "o", [agent.path])
            bus.remove_message_handler(agent.handle)
    except Exception as exc:  # noqa: BLE001 - best-effort: the connect reports the refusal
        return "error", str(exc)
    finally:
        bus.disconnect()


async def is_paired(address: str) -> bool:
    """Whether BlueZ holds a bond for ``address``. ``False`` if the query fails in any way."""
    try:
        bus = await _system_bus()
    except Exception as exc:  # noqa: BLE001 - no bus: nothing is known to be paired
        _log.debug("BlueZ pairing query unavailable: %s", exc)
        return False
    try:
        objects = await _objects(bus)
        path = _device_in(objects, address)
        return path is not None and bool(_value(objects[path][_DEVICE_IFACE], "Paired"))
    except Exception as exc:  # noqa: BLE001 - a status hiccup is not a bond
        _log.debug("BlueZ pairing query failed for %s: %s", address, exc)
        return False
    finally:
        bus.disconnect()


async def unpair(address: str) -> bool:
    """Forget the bond that BlueZ has for ``address``. ``True`` only if a bond was removed."""
    try:
        bus = await _system_bus()
    except Exception as exc:  # noqa: BLE001 - no bus: nothing to remove
        _log.debug("BlueZ unpair unavailable: %s", exc)
        return False
    try:
        objects = await _objects(bus)
        path = _device_in(objects, address)
        if path is None or not _value(objects[path][_DEVICE_IFACE], "Paired"):
            return False
        reply = await _remove(bus, path)
        return reply is not None and not _is_error(reply)
    except Exception as exc:  # noqa: BLE001 - best-effort, like the Windows twin
        _log.debug("BlueZ unpair failed for %s: %s", address, exc)
        return False
    finally:
        bus.disconnect()
