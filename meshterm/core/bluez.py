# SPDX-License-Identifier: Apache-2.0
"""Pair a Bluetooth companion with its PIN through BlueZ, the Linux Bluetooth stack.

A MeshCore companion puts its UART characteristic behind an *authenticated* bond (ENC+MITM),
which on a device with a fixed PIN means Passkey Entry: the computer has to type the six
digits in. On Linux, bleak's ``pair()`` cannot do that. It calls BlueZ's ``Device1.Pair`` and
nothing else, and BlueZ gets a passkey only by asking a **pairing agent** — an object some
program registers on the system bus to answer ``RequestPasskey``. On a desktop that agent is
the Settings panel, in a terminal it is ``bluetoothctl``, over SSH it is usually nobody. With
no agent to ask, BlueZ falls back to "Just Works", the firmware refuses a bond without MITM,
and the PIN MeshTerm was handed never reached the radio at all. That is the Linux twin of the
Windows gap :meth:`~meshterm.core.connection.MeshCoreDevice._pair_ble_windows` closes.

So MeshTerm registers its own agent for the length of one pairing, answering with the PIN it
was given, and calls ``Pair`` from the *same* bus connection. BlueZ routes a pairing's agent
requests to the agent of whoever called ``Pair`` (the system default agent is only the
fallback), so this never takes over the desktop's agent and never answers for any other
device's pairing.

The agent answers raw D-Bus messages (:class:`PinAgent`) rather than going through
``dbus_fast``'s annotated ``ServiceInterface``: that one reads D-Bus signatures out of
annotations like ``device: "o"``, which a linter takes for an undefined name and postponed
annotations turn into a quoted string. A message handler has neither problem and can be
exercised without a bus.

``dbus_fast`` is bleak's own dependency on Linux, so nothing new is installed; it is imported
lazily, and every entry point here is best-effort — a missing system bus or BlueZ reports an
``"error"`` outcome instead of raising.
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
_AGENTS = "/org/bluez"  # where AgentManager1 lives

#: The agent's declared input/output capability. The companion is the side with the PIN
#: (``DisplayOnly``), so we are the keyboard: ``KeyboardOnly`` makes the pairing Passkey
#: Entry with us typing, which is the one method a fixed PIN can satisfy.
AGENT_CAPABILITY = "KeyboardOnly"

#: How long to run discovery for a device BlueZ has forgotten (seconds). BlueZ drops an
#: unpaired device it has stopped hearing after about half a minute, so one the picker
#: listed a while ago may need hearing again before it can be paired.
DISCOVER_S = 6.0

#: Bound on the ``Pair`` call itself (seconds). A healthy passkey pairing takes one to three
#: seconds; this only has to outlast a slow radio, and stays well inside the probe's window.
PAIR_TIMEOUT_S = 15.0

#: Attempts at ``Pair`` when the link under it fails to come up, and the pause between them.
#: A Raspberry Pi class radio drops three to seven link attempts per connect
#: (``le-connection-abort-by-local``, HCI 0x3e). bleak retries those inside its own
#: ``connect()``; BlueZ's ``Pair`` does not, and answers ConnectionAttemptFailed at once.
#: Measured on a uConsole: the first Pair failed that way, the connect then went ahead
#: unpaired, and the protected subscribe cost 32 s before the repair paired it after all.
PAIR_ATTEMPTS = 4
PAIR_RETRY_DELAY_S = 0.5

_LINK_FLAKES = ("org.bluez.Error.ConnectionAttemptFailed",)


def _link_flake(reply: Any) -> bool:
    """Whether a ``Pair`` error is the link failing to come up — worth another try."""
    detail = reply.body[0] if reply.body and isinstance(reply.body[0], str) else ""
    return reply.error_name in _LINK_FLAKES or "le-connection-abort" in detail


_MAC = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


def is_mac(address: str) -> bool:
    """Whether ``address`` is a colon- or dash-separated 48-bit Bluetooth address."""
    return bool(_MAC.match(address or ""))


def _normal(address: str) -> str:
    """An address as BlueZ spells it: upper case, colon-separated."""
    return address.replace("-", ":").upper()


def status_name(error_name: str, body: list[Any] | None = None) -> str:
    """Turn a BlueZ error into the lowercase words a message can quote.

    ``org.bluez.Error.AuthenticationFailed`` → ``"authentication failed"``. The bare
    ``org.bluez.Error.Failed`` says nothing on its own, so its text rides along
    (``"failed: le-connection-abort-by-local"``).

    Args:
        error_name: The D-Bus error name from the reply.
        body: The error reply's body, whose first item is BlueZ's detail string.

    Returns:
        The status, as :data:`~meshterm.core.connection._PAIRING_REFUSED` and its sibling
        sets spell them.
    """
    leaf = error_name.rsplit(".", 1)[-1]
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", leaf).lower()
    detail = body[0] if body and isinstance(body[0], str) else ""
    if words == "failed" and detail:
        return f"failed: {detail}"
    return words


class PinAgent:
    """A BlueZ ``Agent1`` that answers one device's pairing with one PIN — or refuses it.

    Installed as a message handler on the bus that calls ``Pair`` (see the module docstring
    for why that bus), or made BlueZ's default agent for the length of a connect (see
    :func:`answering`). It answers only calls addressed to its own object path, and only for
    the device it was built for; anything else is ``Rejected``, so a pairing we did not start
    can never be approved by us. Without a PIN it refuses every request at once, which is
    the point of it there: the alternative is BlueZ waiting on an agent that doesn't exist.

    It never approves a pairing *without* the passkey (``RequestAuthorization``, the "Just
    Works" yes/no). That is how a companion ends up with an unauthenticated bond — encryption
    works, the UART characteristic still refuses — which is the stale-bond failure itself.

    Attributes:
        path: The object path the agent is registered at.
        asked: Whether BlueZ actually asked for the PIN — a pairing that failed without
            asking was refused before the passkey stage, which is a different story.
    """

    def __init__(self, path: str, pin: str | None, device_path: str) -> None:
        """Build the agent.

        Args:
            path: The object path to answer at.
            pin: The pairing PIN (digits), or ``None`` to refuse every request.
            device_path: The one device it may answer for: its BlueZ object path, or the
                path's ``/dev_…`` tail when the adapter isn't known yet (see
                :func:`device_tail`).
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
            A reply ``Message``, or ``None`` when the message is not ours.
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
            # Numeric Comparison: approve only the number our PIN names. A companion with a
            # fixed PIN never asks for this; one that does and shows another number is not
            # the pairing we were told to make.
            self.asked = True
            if int(msg.body[1]) == int(self._pin):
                return Message.new_method_return(msg)
            return Message.new_error(msg, _REJECTED, "passkey does not match the PIN")
        if member in ("DisplayPasskey", "DisplayPinCode"):
            return Message.new_method_return(msg)
        return Message.new_error(msg, _REJECTED, f"{member} is not something MeshTerm answers")


def device_tail(address: str) -> str:
    """The ``/dev_AA_BB_…`` tail of the object path BlueZ gives the device at ``address``."""
    return "/dev_" + _normal(address).replace(":", "_")


@asynccontextmanager
async def answering(address: str, pin: str | None) -> AsyncIterator[None]:
    """Be BlueZ's default pairing agent for one device while a connect runs.

    BlueZ does not wait to be asked to pair. When the companion refuses the UART subscribe
    with *Insufficient Authentication*, BlueZ raises the link's security by itself and starts
    SMP pairing, which needs an agent — and the one it asks is the **default** agent, not
    ours (that routing is only for a ``Pair`` we call). With none registered, as on any
    headless machine, it offers the companion ``DisplayYesNo``, lands on "Just Works", asks a
    yes/no nobody is there to answer, and the companion hangs up on its 30 s SMP timeout.
    Measured on a uConsole with btmon: 3.3 s to the question, 33.5 s to the hang-up, and only
    then "requires a PIN". On a desktop the question goes to the desktop's dialog instead,
    and answering it makes the unauthenticated bond that later refuses the subscribe.

    So for the length of the connect MeshTerm is the default agent, for this device only,
    declared ``KeyboardOnly`` so the pairing BlueZ starts is Passkey Entry: with a PIN it types
    it in and the connect pairs on the spot; without one it refuses at once and the refusal
    arrives in seconds. Another device's pairing in that window is refused, not left hanging.
    BlueZ keeps default agents as a stack, so unregistering hands the role back to whoever
    held it. Best-effort: with no system bus it simply does nothing.

    Args:
        address: The companion's Bluetooth address.
        pin: Its PIN, or ``None`` to refuse.

    Yields:
        Nothing; the agent is live for the body of the ``async with``.
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
        yield
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
    """Call a BlueZ method and return the raw reply (errors are left for the caller)."""
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
    """Every BlueZ object, from ``GetManagedObjects`` (empty when BlueZ didn't answer)."""
    reply = await _call(bus, "/", _OBJECT_MANAGER_IFACE, "GetManagedObjects")
    if _is_error(reply) or not reply.body:
        return {}
    return reply.body[0]


def _value(props: dict[str, Any], name: str) -> Any:
    """A property's plain value, unwrapping the ``Variant`` BlueZ sends."""
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
    """The first Bluetooth adapter's object path."""
    return next((path for path, ifaces in objects.items() if _ADAPTER_IFACE in ifaces), None)


async def _find_device(bus: Any, address: str, discover_s: float) -> tuple[str | None, bool]:
    """Find the device's object path, listening for it briefly if BlueZ has forgotten it.

    Returns:
        ``(path, paired)`` — ``path`` is ``None`` when it could not be heard.
    """
    objects = await _objects(bus)
    path = _device_in(objects, address)
    if path is None:
        adapter = _adapter_in(objects)
        if adapter is None:
            return None, False
        # A refusal here is fine: bleak or the desktop may already be discovering, which
        # is all we need.
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
    """The adapter a device hangs off — its own ``Adapter`` property, else the first one."""
    own = _value(objects.get(path, {}).get(_DEVICE_IFACE, {}), "Adapter")
    return str(own) if own else _adapter_in(objects)


async def _remove(bus: Any, path: str) -> Any:
    """Forget a device's bond (``Adapter1.RemoveDevice``), which also forgets the device.

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
    """Pair the companion at ``address`` using ``pin``, through our own BlueZ agent.

    Mirrors the Windows ceremony's contract: an existing bond is trusted unless ``force``,
    which tears it down and pairs afresh — the heal for a bond the device has since lost.

    Args:
        address: The companion's Bluetooth address.
        pin: Its pairing PIN (six digits).
        force: Whether to remove an existing bond first.
        discover_s: How long to listen for a device BlueZ has forgotten.
        pair_timeout_s: Bound on the ``Pair`` call.

    Returns:
        ``(outcome, status)`` in :class:`~meshterm.core.connection._BlePairing`'s vocabulary:
        ``"reused"``, ``"paired"``, ``"absent"``, ``"failed"`` with the BlueZ status, or
        ``"error"`` with what went wrong.
    """
    if not pin.isdigit():
        return "failed", "authentication failed"  # a passkey is a number; this can't be it
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
        paired_link: str | None = None  # set once Pair has been asked to open a link
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
                    # Refused before the passkey stage: the PIN was never in question.
                    status = "authentication rejected"
                return "failed", status
            # Trusted lets later connections through without an agent to authorize them.
            from dbus_fast import Variant

            await _call(
                bus, path, _PROPERTIES_IFACE, "Set", "ssv",
                [_DEVICE_IFACE, "Trusted", Variant("b", True)],
            )  # fmt: skip
            return "paired", ""
        finally:
            if paired_link is not None:
                # ``Pair`` opens a link to pair over and BlueZ leaves it up. A companion that
                # is connected stops advertising, and the connect that follows looks the
                # device up by scanning for it, so it never finds it — measured on a
                # uConsole: paired, then "couldn't open a link" after two 30 s tries. Put
                # the link down so the connect meets an advertising device, as it would
                # have without us.
                await _call(bus, paired_link, _DEVICE_IFACE, "Disconnect")
            await _call(bus, _AGENTS, _AGENT_MANAGER_IFACE, "UnregisterAgent", "o", [agent.path])
            bus.remove_message_handler(agent.handle)
    except Exception as exc:  # noqa: BLE001 - best-effort: the connect reports the refusal
        return "error", str(exc)
    finally:
        bus.disconnect()


async def is_paired(address: str) -> bool:
    """Whether BlueZ holds a bond for ``address``. ``False`` on any failure to ask."""
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
    """Forget BlueZ's bond for ``address``. ``True`` only if a bond was removed."""
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
