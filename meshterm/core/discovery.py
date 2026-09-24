# SPDX-License-Identifier: Apache-2.0
"""Companion-device discovery across transports (serial + Bluetooth LE + TCP).

Discovery enumerates the companion kinds that can be *found* automatically:

* **Serial**: USB serial ports via ``pyserial``, flagging the ones whose USB vendor ID
  matches hardware commonly used for LoRa companion devices (ESP32/nRF boards and their
  USB-UART bridges). Nothing is hidden — unrecognized adapters still appear, just sorted
  after the likely candidates.
* **Bluetooth LE**: nearby devices advertising a ``MeshCore*`` name, scanned via ``bleak``.
  Unlike a bare serial adapter, a BLE advert that names itself MeshCore is a strong signal,
  so scanned devices are always treated as likely companions.

A **TCP** companion — a MeshCore device reachable at a network ``host:port`` (a WiFi board,
or a ``meshcored``-style proxy) — has no discovery: it is not attached and does not
advertise, so there is nothing to scan for. It is instead named explicitly (``--tcp``, a
``transport = "tcp"`` profile, or the picker's "add a network device" prompt) and, once
confirmed, remembered like any other companion. :func:`tcp_device` builds the
:class:`DiscoveredDevice` for such an endpoint so the rest of the pipeline (selection,
remembering, the picker) treats it uniformly.

This module has no UI and no persistent state; it only reads what is currently attached or
in range. The companion connection itself still goes through
:class:`~meshterm.core.connection.MeshCoreDevice`, which the ``transport`` field selects.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

_log = logging.getLogger(__name__)

#: Transport discriminators for a :class:`DiscoveredDevice` (also stored/remembered as-is).
TRANSPORT_SERIAL = "serial"
TRANSPORT_BLE = "ble"
TRANSPORT_TCP = "tcp"
#: The built-in simulator, as a device inventory reports it under ``--mock``. Never
#: discovered — there is nothing to find — but a session that promised no real hardware
#: still has exactly one device, and this is what it is called.
TRANSPORT_MOCK = "mock"

#: Default TCP port assumed when a network companion is named as a bare host with no port.
#: 5000 is the conventional MeshCore companion-over-TCP / ``meshcored`` listen port.
DEFAULT_TCP_PORT = 5000

#: BLE advertisements from a MeshCore companion begin with this name prefix; the scan filters
#: to it so unrelated Bluetooth gadgets (headphones, watches, beacons) never clutter the list.
BLE_NAME_PREFIX = "MeshCore"

#: Default seconds to scan for BLE companions. Long enough to hear a nearby device's periodic
#: advertisement, short enough not to stall the startup splash noticeably.
BLE_SCAN_TIMEOUT_S = 5.0

try:  # bleak >= 3 only; the floor in pyproject is 0.22, so this must not be assumed
    from bleak.exc import BleakBluetoothNotAvailableError as _BleakNotAvailable

    _BLE_NOT_AVAILABLE: tuple[type[BaseException], ...] = (_BleakNotAvailable,)
except Exception:  # noqa: BLE001 - older bleak, or bleak absent entirely
    # An empty tuple in ``except`` matches nothing, so older installs fall straight through
    # to the broad handler and behave exactly as they did before.
    _BLE_NOT_AVAILABLE = ()

#: Why the last BLE scan could not run, when the cause is something the reader can act on
#: (Bluetooth off; on macOS, permission not granted to the terminal running us). ``None``
#: when the scan worked — including when it simply heard nothing, which is not a fault.
_ble_unavailable: str | None = None


def ble_unavailable_reason() -> str | None:
    """Why the last BLE scan was refused, if it was, as a sentence fit to show the reader.

    Returns:
        The reason the most recent :func:`discover_ble_devices` could not scan, or ``None``
        if it ran — whether or not it found anything. Reset at the start of every scan, so
        it always describes the latest attempt rather than a stale one.
    """
    return _ble_unavailable


#: Default seconds to watch for one *known* companion's advertisement while waiting for it to
#: come back (see :func:`find_ble_device`). Deliberately longer than the startup scan: nothing
#: is waiting on it, the watch ends the instant the device is heard, and a companion that just
#: powered on can take several advertising intervals to be picked up by the OS.
BLE_PRESENCE_TIMEOUT_S = 8.0

# USB vendor IDs frequently seen on MeshCore/Meshtastic companion hardware and the
# USB-UART bridges they ship with. Used only to name, flag, and sort likely devices.
KNOWN_LORA_VIDS: dict[int, str] = {
    0x303A: "Espressif",  # ESP32-S3 / native USB (e.g. XIAO ESP32-S3)
    0x10C4: "Silicon Labs",  # CP210x UART bridge
    0x1A86: "QinHeng",  # CH340 / CH9102 UART bridge
    0x0403: "FTDI",  # FT232 UART bridge
    0x239A: "Adafruit",  # nRF52840 boards
    0x2886: "Seeed Studio",  # XIAO / Wio boards
    0x1915: "Nordic",  # nRF52 native USB
}

# Native-USB VIDs of LoRa companion dev boards — the chip *is* the board, so seeing one is
# a strong signal the port is companion hardware.
NATIVE_LORA_VIDS: frozenset[int] = frozenset({0x303A, 0x239A, 0x2886, 0x1915})

# Generic USB-UART bridge chips. LoRa boards often use them, but so do countless unrelated
# gadgets (Arduinos, GPS pucks, 3D printers…), so their presence is only a weak hint.
UART_BRIDGE_VIDS: frozenset[int] = frozenset({0x10C4, 0x1A86, 0x0403})

# Sort rank per confidence tier: boards first, then bare serial bridges, then everything else.
_CONFIDENCE_RANK: dict[str, int] = {"board": 0, "bridge": 1, "unknown": 2}


@dataclass(slots=True)
class DiscoveredDevice:
    """A companion device found on the system, with transport metadata.

    A serial device carries its USB metadata; a Bluetooth LE device carries its BLE
    ``address`` and advertised ``name`` instead (its ``port`` is left blank); a TCP device
    carries a network ``host`` and ``tcp_port``. The ``transport`` field says which, and
    :attr:`target` returns the identifier used to open a connection regardless of kind.

    Attributes:
        port: Serial port path (e.g. ``COM5`` or ``/dev/ttyUSB0``); ``""`` for BLE/TCP.
        description: Human-readable description reported by the OS/driver/advert.
        hwid: Raw hardware id string from ``pyserial`` (VID/PID/serial blob); serial only.
        vid: USB vendor ID, if the port is a USB device.
        pid: USB product ID, if the port is a USB device.
        serial_number: USB serial number, if exposed by the device.
        manufacturer: USB manufacturer string, if available.
        product: USB product string, if available.
        transport: ``"serial"``, ``"ble"``, or ``"tcp"`` — the connection layer for this device.
        address: Bluetooth address for a BLE device (e.g. ``AA:BB:CC:DD:EE:FF``).
        name: Advertised BLE local name (e.g. ``"MeshCore-Basestation"``); BLE only.
        ble_device: The live ``bleak.BLEDevice`` the scan produced; BLE only, and only for
            devices discovered *this session* (a remembered device reloads as a bare
            address). Handed to the connection layer so it can open the peripheral
            directly — connecting by address alone makes bleak re-discover the device with
            a fresh internal scan, which on Windows intermittently misses a
            slow-advertising companion. Typed ``object`` so ``bleak`` stays optional; never
            persisted.
        host: Hostname or IP of a TCP companion (e.g. ``"192.168.1.50"``); TCP only.
        tcp_port: TCP port the companion listens on (e.g. ``5000``); TCP only.
    """

    port: str = ""
    description: str = ""
    hwid: str = ""
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    transport: str = TRANSPORT_SERIAL
    address: str | None = None
    name: str | None = None
    ble_device: object | None = None
    host: str | None = None
    tcp_port: int | None = None

    @property
    def is_ble(self) -> bool:
        """Whether this device is reached over Bluetooth LE rather than a serial port."""
        return self.transport == TRANSPORT_BLE

    @property
    def is_tcp(self) -> bool:
        """Whether this device is reached over a TCP network connection."""
        return self.transport == TRANSPORT_TCP

    @property
    def target(self) -> str:
        """The identifier a connection opens on.

        The network ``host:port`` for TCP, the BLE address for Bluetooth, else the
        serial port.
        """
        if self.is_tcp:
            return f"{self.host}:{self.tcp_port}"
        if self.transport == TRANSPORT_MOCK:
            return "--mock"  # the flag is the target: it is how this device is selected
        return self.address or self.port if self.is_ble else self.port

    @property
    def stable_id(self) -> str:
        """Return an identifier stable across replug/reboot.

        For TCP, the network ``host:port``. For BLE, the Bluetooth address (stable per
        adapter pairing). For serial, the USB serial number (survives a changed COM
        number), then the ``vid:pid`` pair, and finally the port name as a last resort.

        Returns:
            A non-empty identifier string used to recognize this device later.
        """
        if self.is_tcp:
            return f"tcp:{self.host}:{self.tcp_port}"
        if self.transport == TRANSPORT_MOCK:
            return "mock:simulator"
        if self.is_ble:
            return f"ble:{(self.address or self.name or '').lower()}"
        if self.serial_number:
            return f"sn:{self.serial_number}"
        if self.vid is not None and self.pid is not None:
            return f"vidpid:{self.vid:04x}:{self.pid:04x}"
        return f"port:{self.port}"

    @property
    def confidence(self) -> str:
        """How likely this device is a LoRa companion.

        Returns:
            ``"board"`` for a BLE device that named itself MeshCore, a TCP endpoint the user
            named explicitly, or a LoRa dev board's native USB (all strong signals),
            ``"bridge"`` for a generic USB-UART chip (a weak hint — could be anything), or
            ``"unknown"`` for an unrecognized adapter.
        """
        if self.is_ble or self.is_tcp:
            return "board"  # a MeshCore BLE advert / a user-named TCP endpoint is confident
        if self.vid in NATIVE_LORA_VIDS:
            return "board"
        if self.vid in UART_BRIDGE_VIDS:
            return "bridge"
        return "unknown"

    @property
    def is_likely_lora(self) -> bool:
        """Whether this looks like a companion: a MeshCore BLE advert or a known-VID board."""
        return self.confidence != "unknown"

    @property
    def vendor_label(self) -> str:
        """The hardware maker's name for the VENDOR column, or ``""`` when unknown.

        This is deliberately *just the vendor* — the transport (USB vs Bluetooth vs TCP) is a
        separate concern shown in its own TYPE column, so a BLE advert or TCP endpoint (which
        rarely expose a maker) reports whatever manufacturer string it carries and otherwise
        stays blank rather than mislabelling the transport as a vendor. A serial port maps
        its USB vendor ID to a friendly name, falling back to the driver's manufacturer
        string.
        """
        if self.is_ble or self.is_tcp:
            return self.manufacturer or ""
        if self.vid in KNOWN_LORA_VIDS:
            return KNOWN_LORA_VIDS[self.vid]
        return self.manufacturer or ""

    @property
    def label(self) -> str:
        """A concise human-friendly label, e.g. ``"Wio SX1262 (COM5)"`` or ``"…(BLE)"``.

        The OS description often already ends with the port (Windows reports
        ``"USB Serial Device (COM11)"``), so the identifier is not appended a second time.
        """
        if self.is_tcp:
            name = self.name or self.product or self.description or "Network device"
            return f"{name} ({self.target})"
        if self.is_ble:
            name = self.name or self.product or self.description or "Bluetooth device"
            return f"{name} (BLE)"
        name = self.product or self.description or self.vendor_label or "Serial device"
        if not self.port:
            return name
        suffix = f"({self.port})"
        if name.endswith(suffix):  # avoid "… (COM11) (COM11)"
            name = name[: -len(suffix)].rstrip()
        return f"{name} ({self.port})"


def parse_tcp_endpoint(text: str, *, default_port: int = DEFAULT_TCP_PORT) -> tuple[str, int]:
    """Parse a ``host[:port]`` string into a ``(host, port)`` pair.

    Accepts a bare host (``"192.168.1.50"``, ``"meshcore.local"``) — defaulting the port to
    ``default_port`` — or an explicit ``host:port``. IPv6 literals must be bracketed
    (``"[::1]:5000"``) so the address colons aren't mistaken for the port separator.

    Args:
        text: The user-supplied network address.
        default_port: Port assumed when ``text`` names only a host.

    Returns:
        The ``(host, port)`` to connect to.

    Raises:
        ValueError: If the host is empty or the port isn't a valid 1–65535 integer — with a
            message already phrased for the user.
    """
    raw = text.strip()
    if not raw:
        raise ValueError("Enter a network address, e.g. 192.168.1.50 or 192.168.1.50:5000.")
    host, sep, port_text = _split_host_port(raw)
    host = host.strip()
    if not host:
        raise ValueError("Enter a host, e.g. 192.168.1.50 or 192.168.1.50:5000.")
    if not sep or not port_text.strip():
        return host, default_port
    try:
        port = int(port_text.strip())
    except ValueError:
        raise ValueError(f"'{port_text.strip()}' isn't a valid port number.") from None
    if not 1 <= port <= 65535:
        raise ValueError("The port must be between 1 and 65535.")
    return host, port


def _split_host_port(raw: str) -> tuple[str, str, str]:
    """Split ``host[:port]`` into ``(host, separator, port)``, honouring ``[ipv6]`` brackets.

    A bracketed IPv6 literal keeps its colons; only a ``:port`` *after* the closing bracket
    (or the single colon of a plain ``host:port``) is treated as the port separator.
    """
    if raw.startswith("["):
        close = raw.find("]")
        if close != -1:
            host = raw[1:close]
            rest = raw[close + 1 :]
            if rest.startswith(":"):
                return host, ":", rest[1:]
            return host, "", ""
    host, sep, port_text = raw.rpartition(":")
    if not sep:  # no colon at all → the whole string is the host
        return port_text, "", ""
    return host, sep, port_text


def tcp_device(host: str, port: int, *, name: str = "") -> DiscoveredDevice:
    """Build the :class:`DiscoveredDevice` for a TCP companion at ``host:port``.

    TCP companions aren't scanned for, so this is how a network endpoint enters the same
    selection/remember/picker pipeline the discovered transports use. An optional ``name``
    (a friendly label the user typed, or a mesh node name learned on a prior connect) is
    carried through to the picker's DEVICE column.

    Args:
        host: Hostname or IP of the companion.
        port: TCP port it listens on.
        name: Optional friendly name for display.

    Returns:
        A TCP :class:`DiscoveredDevice` ready to select or connect.
    """
    return DiscoveredDevice(
        transport=TRANSPORT_TCP,
        host=host,
        tcp_port=port,
        name=name or None,
        description=name,
        product=name or None,
    )


def serial_device(port: str, *, name: str = "") -> DiscoveredDevice:
    """Build the :class:`DiscoveredDevice` for a serial companion at ``port``.

    A soldered platform-bus UART (e.g. ``/dev/ttyS1`` on the Luckfox Lyra) is not enumerated by
    pyserial, so a scan never finds it; this is how a configured serial ``port`` enters the same
    selection/remember/picker pipeline the discovered transports use — mirroring
    :func:`tcp_device` for network companions. An optional ``name`` (the profile alias, or a
    mesh node name learned on a prior connect) is carried to the picker's DEVICE column.

    Args:
        port: Serial port path (e.g. ``/dev/ttyS1`` or ``COM5``).
        name: Optional friendly name for display.

    Returns:
        A serial :class:`DiscoveredDevice` ready to select or connect.
    """
    return DiscoveredDevice(
        port=port,
        description=name,
        product=name or None,
    )


def _sort_key(device: DiscoveredDevice) -> tuple[int, str]:
    """Sort likely companions first, then bridges, then unknown; ties by target id."""
    return (_CONFIDENCE_RANK[device.confidence], device.target)


def discover_devices() -> list[DiscoveredDevice]:
    """Enumerate serial ports currently attached to the system.

    Likely LoRa companion devices (by known USB vendor ID) are returned first. If
    ``pyserial`` is unavailable the function returns an empty list rather than raising,
    so callers can fall back to ``--port``/``--mock`` with a friendly message.

    Returns:
        Discovered serial devices, likely candidates first, then sorted by port name.
    """
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover - pyserial is a declared dependency
        return []

    devices = [
        DiscoveredDevice(
            port=info.device,
            description=info.description or "",
            hwid=info.hwid or "",
            vid=info.vid,
            pid=info.pid,
            serial_number=info.serial_number,
            manufacturer=info.manufacturer,
            product=info.product,
        )
        for info in list_ports.comports()
    ]
    devices.sort(key=_sort_key)
    return devices


async def discover_ble_devices(timeout: float = BLE_SCAN_TIMEOUT_S) -> list[DiscoveredDevice]:
    """Scan for nearby Bluetooth LE companions advertising a ``MeshCore*`` name.

    Uses ``bleak`` to listen for advertisements for ``timeout`` seconds and keeps only the
    ones whose advertised name marks them as a MeshCore companion. If ``bleak`` is
    unavailable, or the platform has no usable Bluetooth adapter, this returns an empty list
    rather than raising — BLE is an optional transport, and its absence must never block
    serial discovery or the simulator.

    Args:
        timeout: Seconds to scan for advertising devices.

    Returns:
        Discovered BLE companions (deduplicated by address), sorted by name.
    """
    global _ble_unavailable
    _ble_unavailable = None

    try:
        from bleak import BleakScanner
    except ImportError:  # bleak not installed → BLE simply unavailable
        return []

    try:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except _BLE_NOT_AVAILABLE as exc:
        # A *state the reader can fix* — Bluetooth switched off, or (on macOS, routinely)
        # the terminal running us has not been granted Bluetooth. That is not the "no
        # adapter / driver hiccup" the broad catch below is for, and returning [] silently
        # made it indistinguishable from "nothing is nearby": the one message that would
        # have told the user what to do was thrown away at debug level, under a log that
        # defaults to WARNING. Keep the sentence bleak wrote — it already names the remedy.
        _ble_unavailable = str(getattr(exc, "args", [None])[0] or exc)
        _log.warning("BLE unavailable: %s", _ble_unavailable)
        return []
    except Exception as exc:  # noqa: BLE001 - no adapter / OS Bluetooth off / driver hiccup
        _log.debug("BLE scan unavailable: %s", exc)
        return []

    devices: list[DiscoveredDevice] = []
    for dev, adv in found.values():
        name = (getattr(adv, "local_name", None) or getattr(dev, "name", None) or "").strip()
        if not name.startswith(BLE_NAME_PREFIX):
            continue
        devices.append(
            DiscoveredDevice(
                transport=TRANSPORT_BLE,
                address=dev.address,
                name=name,
                description=name,
                product=name,
                ble_device=dev,
            )
        )
    devices.sort(key=lambda d: (d.name or "").casefold())
    return devices


async def find_ble_device(address: str, timeout: float = BLE_PRESENCE_TIMEOUT_S) -> object | None:
    """Watch for one known companion's advertisement, returning its live ``BLEDevice``.

    The Bluetooth counterpart to :func:`~meshterm.core.connection.serial_port_present`, and it
    answers the same question the reconnect flow asks of a serial port: *is the device back
    yet?* Unlike a serial port there is no OS registry to consult — a peripheral exists only
    while it is advertising — so the probe is a scan. ``find_device_by_address`` returns the
    **moment** the address is heard rather than at the end of the window, so a generous
    ``timeout`` costs nothing when the device is present and simply keeps listening when it
    isn't.

    The returned handle matters as much as the answer. A companion that has been power-cycled
    is a *new* peripheral to the OS: the ``BLEDevice`` captured by an earlier scan names a
    connection endpoint that no longer resolves, and on Windows handing bleak that stale object
    fails the connect outright. Answering the presence question with a freshly-scanned handle
    means the reconnect that follows opens the device that is actually there (see
    :meth:`~meshterm.context.AppContext.reconnect`).

    Never raises: a missing ``bleak``, a disabled adapter, or a driver hiccup reads as "not
    advertising", which is the truthful answer for a device we cannot currently reach.

    Args:
        address: The Bluetooth address to listen for (as reported by discovery).
        timeout: Seconds to keep listening before giving up on this round.

    Returns:
        The live ``bleak.BLEDevice`` if the address advertised within ``timeout``, else
        ``None``. Typed ``object`` so ``bleak`` stays an optional dependency.
    """
    try:
        from bleak import BleakScanner
    except ImportError:  # bleak not installed → BLE simply unavailable
        return None

    try:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - no adapter / OS Bluetooth off / driver hiccup
        _log.debug("BLE presence scan for %s unavailable: %s", address, exc)
        return None


async def discover_all(
    *, ble: bool = True, ble_timeout: float = BLE_SCAN_TIMEOUT_S
) -> list[DiscoveredDevice]:
    """Enumerate every attached or in-range companion across all transports.

    Serial enumeration is instant; the BLE scan is what takes ``ble_timeout`` seconds. Serial
    devices are listed first (they're already attached and connect fastest), then BLE
    companions.

    Args:
        ble: Whether to include a Bluetooth LE scan (skip it to avoid the scan delay).
        ble_timeout: Seconds to spend scanning for BLE companions.

    Returns:
        All discovered devices: serial (likely-LoRa first), then BLE companions.
    """
    # ``comports()`` is a blocking SetupAPI/sysfs walk — hundreds of milliseconds on
    # Windows, and long enough on a slow console to freeze whatever spinner is covering
    # this call. Off the event loop, so the animation reporting the wait keeps running.
    serial = await asyncio.to_thread(discover_devices)
    if not ble:
        return serial
    # A device paired over both USB and BLE is vanishingly unlikely to collide by stable_id
    # (USB serial number vs BLE address), so no cross-transport dedup is needed here.
    ble_devices = await discover_ble_devices(ble_timeout)
    return serial + ble_devices
