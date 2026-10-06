# SPDX-License-Identifier: Apache-2.0
"""Device selection without user interaction: change discovery and memory into a chosen port.

The scripted CLI path uses this logic. It is also the fallback when the interactive picker
is not available. It is pure and has no UI. It takes the devices that are discovered now,
the remembered default, and the explicit overrides. Then it returns a :class:`Resolution`,
or it raises :class:`DeviceSelectionError` with a message for the user that is ready to
print.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import DeviceProfile
from .device_store import RememberedDevice
from .discovery import (
    TRANSPORT_BLE,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
    DiscoveredDevice,
    parse_tcp_endpoint,
    tcp_device,
)


class DeviceSelectionError(ValueError):
    """Raised when MeshTerm cannot choose one device without ambiguity.

    The message is already formatted for the user. It lists the discovered devices, and
    tells how to remove the ambiguity. Thus callers can print ``str(exc)`` directly.
    """


@dataclass(slots=True)
class Resolution:
    """The result of the decision about which device to use.

    Attributes:
        port: The chosen connection target: the serial port for a serial device, or the
            Bluetooth address for a BLE device. (Its name is ``port`` for historical
            reasons. Use :attr:`target`.)
        device: The discovered device that matches, when the enumeration knows it (so
            that the caller can remember it after a successful connection). ``None`` for
            an explicit target that the enumeration does not find now.
        source: Where the choice came from (``"port"``, ``"ble"``, ``"tcp"``, ``"profile"``,
            ``"remembered"``, or ``"only"``), for logs and messages.
        transport: ``"serial"``, ``"ble"``, or ``"tcp"``: the connection layer for the
            device.
    """

    port: str
    device: DiscoveredDevice | None
    source: str
    transport: str = TRANSPORT_SERIAL

    @property
    def target(self) -> str:
        """The connection target (serial port or BLE address), another name for :attr:`port`."""
        return self.port


def _find_by_target(devices: list[DiscoveredDevice], target: str) -> DiscoveredDevice | None:
    """Return the discovered device whose port or BLE address is ``target``."""
    return next((d for d in devices if d.target == target or d.port == target), None)


def _resolve_tcp(endpoint: str, devices: list[DiscoveredDevice], source: str) -> Resolution:
    """Build a TCP :class:`Resolution` from a ``host[:port]`` string.

    The function normalizes the endpoint (a bare host gets the default port). Thus the
    target is the same as the target that a remembered TCP device stores. Discovery cannot
    find a TCP companion. Thus an entry in ``devices`` that matches can only be a remembered
    device that the caller added.

    Raises:
        DeviceSelectionError: If ``endpoint`` is not a valid ``host[:port]``.
    """
    try:
        host, port = parse_tcp_endpoint(endpoint)
    except ValueError as exc:
        raise DeviceSelectionError(str(exc)) from exc
    target = f"{host}:{port}"
    match = _find_by_target(devices, target) or tcp_device(host, port)
    return Resolution(target, match, source, TRANSPORT_TCP)


def _format_device_list(devices: list[DiscoveredDevice]) -> str:
    """Render the discovered devices as an indented bullet list for the user."""
    if not devices:
        return "  no serial devices detected"
    lines = []
    for d in devices:
        flag = " [likely LoRa]" if d.is_likely_lora else ""
        kind = "TCP" if d.is_tcp else "BLE" if d.is_ble else "serial"
        lines.append(f"  • {d.target} ({kind}) — {d.product or d.description or 'device'}{flag}")
    return "\n".join(lines)


def resolve_device(
    devices: list[DiscoveredDevice],
    remembered: RememberedDevice | None,
    *,
    explicit_port: str | None = None,
    explicit_ble: str | None = None,
    explicit_tcp: str | None = None,
    profile: DeviceProfile | None = None,
) -> Resolution:
    """Decide to which companion to connect, without a prompt.

    The order of priority:

    1. ``explicit_tcp`` (an explicit ``--tcp`` network address).
    2. ``explicit_ble`` (an explicit ``--ble`` Bluetooth address).
    3. ``explicit_port`` (an explicit ``--port``).
    4. The ``host:port`` of a TCP profile, or the ``port`` of a serial profile.
    5. The remembered "last known good" device, if it is attached or in range now.
    6. The single likely-LoRa device, if exactly one is present. That is a port whose USB
       vendor marks it as a board or a bridge, or a BLE or TCP endpoint. This step ignores
       the ports that look like nothing in particular. The reason: a platform can show
       some such ports always (each Mac has two). Without this rule, they make the count
       ambiguous permanently.
    7. The single attached device, if exactly one is present and no device looked likely.
       Thus a real adapter that MeshTerm does not recognize still resolves.

    If no step chooses a device, the function raises a :class:`DeviceSelectionError` that
    lists the candidates. These are the likely devices if there are any. Else they are all
    the attached devices, under a message that does not claim that they are companions.

    Args:
        devices: The devices that are discovered now (serial, BLE, or both).
        remembered: The remembered default, if there is one.
        explicit_port: A serial port given on the command line.
        explicit_ble: A Bluetooth address given on the command line.
        explicit_tcp: A network ``host[:port]`` given on the command line.
        profile: A device profile given on the command line.

    Returns:
        A :class:`Resolution` that names the chosen target and transport.

    Raises:
        DeviceSelectionError: If MeshTerm cannot choose one device without ambiguity, or
            if it could not parse an explicit TCP endpoint.
    """
    if explicit_tcp:
        return _resolve_tcp(explicit_tcp, devices, "tcp")

    if explicit_ble:
        return Resolution(
            explicit_ble, _find_by_target(devices, explicit_ble), "ble", TRANSPORT_BLE
        )

    if explicit_port:
        match = _find_by_target(devices, explicit_port)
        return Resolution(explicit_port, match, "port", TRANSPORT_SERIAL)

    if profile is not None and profile.is_tcp and profile.tcp_endpoint:
        return _resolve_tcp(profile.tcp_endpoint, devices, "profile")

    if profile is not None and profile.port:
        match = _find_by_target(devices, profile.port)
        return Resolution(profile.port, match, "profile", TRANSPORT_SERIAL)

    if remembered is not None:
        match = next((d for d in devices if remembered.matches(d)), None)
        if match is not None:
            return Resolution(match.target, match, "remembered", match.transport)

    # Prefer the devices that look like companions. This is important because of macOS.
    # Each Mac always shows /dev/cu.Bluetooth-Incoming-Port and /dev/cu.debug-console.
    # These device files have no USB VID/PID, so their score is "unknown". When MeshTerm
    # counted them, `len(devices) == 1` was never true on a Mac. A Mac with exactly one
    # real board attached found three devices and refused to choose. Thus auto-detection
    # could never work on that platform, and each Mac user got "Multiple companion devices
    # detected" at the first run. MeshTerm already knows which device is plausible, and
    # the listing below already prints it as "[likely LoRa]". This code uses that
    # knowledge to decide, not only to show it.
    likely = [d for d in devices if d.is_likely_lora]

    if len(likely) == 1:
        return Resolution(likely[0].target, likely[0], "only", likely[0].transport)

    # Nothing is recognizable: use the full list, instead of a list that is narrowed to
    # nothing. Thus a single adapter that MeshTerm does not recognize still connects
    # exactly as it always did. (Such an adapter is a UART bridge with a VID that is not
    # in our list, and usually it is a real board.)
    if not likely and len(devices) == 1:
        return Resolution(devices[0].target, devices[0], "only", devices[0].transport)

    if not devices:
        raise DeviceSelectionError(
            "No companion devices detected. Connect a companion device (USB or Bluetooth), "
            "or pass --port / --ble explicitly, or use --mock for the simulator."
        )

    if not likely:
        # Several ports are attached, but no port is plausible. "Multiple companion
        # devices" is false here. On a bare Mac, it names two virtual ports that are not
        # companions, and then tells the user to choose one of them.
        raise DeviceSelectionError(
            "No companion devices detected among the attached ports.\n"
            f"{_format_device_list(devices)}\n"
            "Connect a companion device (USB or Bluetooth), or pass --port / --ble "
            "explicitly if one of these is your companion, or use --mock for the simulator."
        )

    raise DeviceSelectionError(
        "Multiple companion devices detected and no default to fall back on.\n"
        f"{_format_device_list(likely)}\n"
        "Choose one with --port <PORT> or --ble <ADDRESS> (or run 'meshterm devices' to "
        "inspect them). The chosen device is remembered as the default after it connects."
    )
