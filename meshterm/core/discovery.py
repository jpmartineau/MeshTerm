# SPDX-License-Identifier: Apache-2.0
"""Discovery of companion devices across transports (serial, Bluetooth LE, and TCP).

Discovery enumerates the types of companion that MeshTerm can find automatically:

* **Serial**: USB serial ports, through ``pyserial``. Discovery flags the ports whose USB
  vendor ID matches hardware that is frequently used for LoRa companion devices (ESP32
  and nRF boards, and their USB-UART bridges). Nothing is hidden. Unrecognized adapters
  still appear, but they are sorted after the probable candidates.
* **Bluetooth LE**: nearby devices that advertise a ``MeshCore*`` name, scanned through
  ``bleak``. A bare serial adapter is a weak signal. But a BLE advertisement that names
  itself MeshCore is a strong signal, so MeshTerm always treats scanned devices as
  probable companions.

A **TCP** companion has no discovery. It is a MeshCore device that is reachable at a
network ``host:port`` (a WiFi board, or a ``meshcored``-style proxy). It is not attached
and does not advertise, so there is nothing to scan for. Instead, the user names it
explicitly (``--tcp``, a ``transport = "tcp"`` profile, or the "add a network device"
prompt of the picker). After it is confirmed, MeshTerm remembers it like any other
companion. :func:`tcp_device` builds the :class:`DiscoveredDevice` for such an endpoint.
Thus the rest of the pipeline (selection, the remembered devices, the picker) treats it
in the same way.

This module has no UI and no persistent state. It only reads what is attached or in
range now. The companion connection itself still goes through
:class:`~meshterm.core.connection.MeshCoreDevice`, which the ``transport`` field selects.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

_log = logging.getLogger(__name__)

#: The transport discriminators for a :class:`DiscoveredDevice` (also stored and
#: remembered unchanged).
TRANSPORT_SERIAL = "serial"
TRANSPORT_BLE = "ble"
TRANSPORT_TCP = "tcp"
#: A LoRa chip on the SPI bus of the host itself, run by a node that MeshTerm starts itself
#: (:mod:`meshterm.core.spiradio`). MeshTerm finds it by its ``/dev/spidev*`` device file,
#: the same as a serial port.
TRANSPORT_SPI = "spi"
#: The built-in simulator, as a device inventory reports it under ``--mock``. Discovery
#: never finds it, because there is nothing to find. But a session that promised no real
#: hardware still has exactly one device, and this is its name.
TRANSPORT_MOCK = "mock"

#: The default TCP port, when a network companion is named as a bare host with no port.
#: 5000 is the conventional listen port of a MeshCore companion over TCP and of
#: ``meshcored``.
DEFAULT_TCP_PORT = 5000

#: The BLE advertisements of a MeshCore companion start with this name prefix. The scan
#: keeps only these, so that other Bluetooth gadgets (headphones, watches, beacons) never
#: fill the list.
BLE_NAME_PREFIX = "MeshCore"

#: The default time, in seconds, of a scan for BLE companions. It is long enough to hear
#: the periodic advertisement of a nearby device, and short enough that the startup splash
#: does not stop for a time that the user can see.
BLE_SCAN_TIMEOUT_S = 5.0

try:  # bleak >= 3 only. The floor in pyproject is 0.22, so do not assume it
    from bleak.exc import BleakBluetoothNotAvailableError as _BleakNotAvailable

    _BLE_NOT_AVAILABLE: tuple[type[BaseException], ...] = (_BleakNotAvailable,)
except Exception:  # noqa: BLE001 - older bleak, or bleak absent entirely
    # An empty tuple in ``except`` matches nothing. Thus older installs go directly to the
    # broad handler, and behave exactly as they did before.
    _BLE_NOT_AVAILABLE = ()

#: Why the last BLE scan could not run, when the user can act on the cause (Bluetooth is
#: off, or, on macOS, the terminal that runs us does not have the permission). ``None``
#: when the scan worked. This includes a scan that heard nothing, which is not a fault.
_ble_unavailable: str | None = None


def ble_unavailable_reason() -> str | None:
    """Why the last BLE scan was refused, if it was, as a sentence to show to the user.

    Returns:
        The reason why the most recent :func:`discover_ble_devices` could not scan, or
        ``None`` if it ran (with or without a result). Each scan resets it at its start,
        so it always describes the latest try, not an old one.
    """
    return _ble_unavailable


#: The default time, in seconds, to watch for the advertisement of one known companion,
#: while MeshTerm waits for it to come back (refer to :func:`find_ble_device`). It is
#: longer than the startup scan on purpose. Nothing waits on it, the watch stops at the
#: moment the device is heard, and the OS can need several advertising intervals to find
#: a companion that was just switched on.
BLE_PRESENCE_TIMEOUT_S = 8.0

# USB vendor IDs that occur frequently on MeshCore and Meshtastic companion hardware, and on
# the USB-UART bridges that come with it. MeshTerm uses them only to name, flag, and sort
# the probable devices.
KNOWN_LORA_VIDS: dict[int, str] = {
    0x303A: "Espressif",  # ESP32-S3 / native USB (for example XIAO ESP32-S3)
    0x10C4: "Silicon Labs",  # CP210x UART bridge
    0x1A86: "QinHeng",  # CH340 / CH9102 UART bridge
    0x0403: "FTDI",  # FT232 UART bridge
    0x239A: "Adafruit",  # nRF52840 boards
    0x2886: "Seeed Studio",  # XIAO / Wio boards
    0x1915: "Nordic",  # nRF52 native USB
}

# Native-USB VIDs of LoRa companion dev boards. The chip is the board, so one of these
# VIDs is a strong signal that the port is companion hardware.
NATIVE_LORA_VIDS: frozenset[int] = frozenset({0x303A, 0x239A, 0x2886, 0x1915})

# Generic USB-UART bridge chips. LoRa boards often use them, but very many unrelated
# gadgets also use them (Arduinos, GPS pucks, 3D printers…). Thus such a chip is only a
# weak hint.
UART_BRIDGE_VIDS: frozenset[int] = frozenset({0x10C4, 0x1A86, 0x0403})

# The sort rank of each confidence tier: boards first, then bare serial bridges, then all
# others.
_CONFIDENCE_RANK: dict[str, int] = {"board": 0, "bridge": 1, "unknown": 2}


@dataclass(slots=True)
class DiscoveredDevice:
    """A companion device found on the system, with its transport metadata.

    A serial device has its USB metadata. A Bluetooth LE device has its BLE ``address``
    and advertised ``name`` instead (its ``port`` is blank). A TCP device has a network
    ``host`` and ``tcp_port``. The ``transport`` field tells which type it is.
    :attr:`target` returns the identifier that opens a connection, for all types.

    Attributes:
        port: The serial port path (for example ``COM5`` or ``/dev/ttyUSB0``). ``""`` for
            BLE and TCP.
        description: The description for people, as the OS, the driver, or the
            advertisement reports it.
        hwid: The raw hardware id string from ``pyserial`` (VID, PID, and serial blob).
            Serial only.
        vid: The USB vendor ID, if the port is a USB device.
        pid: The USB product ID, if the port is a USB device.
        serial_number: The USB serial number, if the device shows it.
        manufacturer: The USB manufacturer string, if available.
        product: The USB product string, if available.
        transport: ``"serial"``, ``"ble"``, ``"tcp"``, or ``"spi"``: the connection layer.
        address: The Bluetooth address of a BLE device (for example
            ``AA:BB:CC:DD:EE:FF``).
        name: The advertised BLE local name (for example ``"MeshCore-Basestation"``).
            BLE only.
        ble_device: The live ``bleak.BLEDevice`` that the scan made. BLE only, and only
            for devices discovered in this session (a remembered device loads again as a
            bare address). MeshTerm gives it to the connection layer, so that the layer
            can open the peripheral directly. A connection by address alone makes bleak
            discover the device again with a new internal scan. On Windows, that scan
            sometimes misses a companion that advertises slowly. Typed ``object``, so that
            ``bleak`` stays optional. Never stored.
        host: The hostname or IP of a TCP companion (for example ``"192.168.1.50"``). TCP
            only.
        tcp_port: The TCP port that the companion listens on (for example ``5000``). TCP
            only.
        spi: The :class:`~meshterm.core.config.SpiWiring` of a radio on the SPI bus of the
            host. SPI only. Its ``port`` is the SPI device file (``/dev/spidev1.0``).
            MeshTerm names and remembers such a radio by this file. Typed ``object``, so
            that this module does not import the config module.
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
    spi: object | None = None

    @property
    def is_spi(self) -> bool:
        """Whether this device is a radio on the host's own SPI bus, run by MeshTerm's node."""
        return self.transport == TRANSPORT_SPI

    @property
    def is_ble(self) -> bool:
        """Whether this device is reached over Bluetooth LE instead of a serial port."""
        return self.transport == TRANSPORT_BLE

    @property
    def is_tcp(self) -> bool:
        """Whether this device is reached over a TCP network connection."""
        return self.transport == TRANSPORT_TCP

    @property
    def target(self) -> str:
        """The identifier on which a connection opens.

        The network ``host:port`` for TCP, the BLE address for Bluetooth, else the
        serial port.
        """
        if self.is_tcp:
            return f"{self.host}:{self.tcp_port}"
        if self.transport == TRANSPORT_MOCK:
            return "--mock"  # the flag is the target, because the flag selects this device
        return self.address or self.port if self.is_ble else self.port

    @property
    def stable_id(self) -> str:
        """Return an identifier that stays the same after a replug or a reboot.

        For TCP: the network ``host:port``. For BLE: the Bluetooth address (stable for
        each adapter pairing). For serial: the USB serial number (it stays when the COM
        number changes), then the ``vid:pid`` pair, and at the end the port name as a
        last resort.

        Returns:
            A non-empty identifier string, to recognize this device later.
        """
        if self.is_tcp:
            return f"tcp:{self.host}:{self.tcp_port}"
        if self.transport == TRANSPORT_MOCK:
            return "mock:simulator"
        if self.is_spi:
            return f"spi:{self.port}"
        if self.is_ble:
            return f"ble:{(self.address or self.name or '').lower()}"
        if self.serial_number:
            return f"sn:{self.serial_number}"
        if self.vid is not None and self.pid is not None:
            return f"vidpid:{self.vid:04x}:{self.pid:04x}"
        return f"port:{self.port}"

    @property
    def confidence(self) -> str:
        """How probable it is that this device is a LoRa companion.

        Returns:
            ``"board"`` for a BLE device that named itself MeshCore, a TCP endpoint that
            the user named explicitly, or the native USB of a LoRa dev board (all strong
            signals). ``"bridge"`` for a generic USB-UART chip (a weak hint, because it
            can be anything). ``"unknown"`` for an unrecognized adapter.
        """
        if self.is_ble or self.is_tcp or self.is_spi:
            return "board"  # a MeshCore BLE name, a named endpoint, or a wired radio: confident
        if self.vid in NATIVE_LORA_VIDS:
            return "board"
        if self.vid in UART_BRIDGE_VIDS:
            return "bridge"
        return "unknown"

    @property
    def is_likely_lora(self) -> bool:
        """Whether this looks like a companion.

        That is, a MeshCore BLE advertisement, or a board with a known VID.
        """
        return self.confidence != "unknown"

    @property
    def vendor_label(self) -> str:
        """The name of the hardware maker for the VENDOR column, or ``""`` when unknown.

        On purpose, this is only the vendor. The transport (USB, Bluetooth, or TCP) is a
        different concern, which shows in its own TYPE column. A BLE advertisement or a TCP
        endpoint seldom shows a maker. Thus it reports the manufacturer string that it has,
        if any, and else it stays blank. It does not show the transport incorrectly as a
        vendor. A serial port maps its USB vendor ID to a friendly name. If the ID is not
        known, the port uses the manufacturer string of the driver.
        """
        if self.is_ble or self.is_tcp or self.is_spi:
            return self.manufacturer or ""
        if self.vid in KNOWN_LORA_VIDS:
            return KNOWN_LORA_VIDS[self.vid]
        return self.manufacturer or ""

    @property
    def label(self) -> str:
        """A short label for people, for example ``"Wio SX1262 (COM5)"`` or ``"…(BLE)"``.

        The OS description often ends with the port already (Windows reports
        ``"USB Serial Device (COM11)"``). Thus the identifier is not added a second time.
        """
        if self.is_tcp:
            name = self.name or self.product or self.description or "Network device"
            return f"{name} ({self.target})"
        if self.is_ble:
            name = self.name or self.product or self.description or "Bluetooth device"
            return f"{name} (BLE)"
        if self.is_spi:
            return f"{self.name or 'SPI radio'} ({self.port})"
        name = self.product or self.description or self.vendor_label or "Serial device"
        if not self.port:  # the simulator has no port to show, so no "()"
            return name
        suffix = f"({self.port})"
        if name.endswith(suffix):  # avoid "… (COM11) (COM11)"
            name = name[: -len(suffix)].rstrip()
        return f"{name} ({self.port})"


def parse_tcp_endpoint(text: str, *, default_port: int = DEFAULT_TCP_PORT) -> tuple[str, int]:
    """Parse a ``host[:port]`` string into a ``(host, port)`` pair.

    Accepts a bare host (``"192.168.1.50"``, ``"meshcore.local"``), with ``default_port``
    as the port, or an explicit ``host:port``. An IPv6 literal must be in brackets
    (``"[::1]:5000"``), so that the colons of the address are not read as the port
    separator.

    Args:
        text: The network address that the user gave.
        default_port: The port to use when ``text`` names only a host.

    Returns:
        The ``(host, port)`` to connect to.

    Raises:
        ValueError: If the host is empty, or the port is not a valid integer from 1 to
            65535. The message is already written for the user.
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
    """Split ``host[:port]`` into ``(host, separator, port)``, and obey ``[ipv6]`` brackets.

    An IPv6 literal in brackets keeps its colons. Only a ``:port`` after the closing
    bracket (or the single colon of a plain ``host:port``) is the port separator.
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

    MeshTerm does not scan for TCP companions. Thus a network endpoint comes through this
    function into the same pipeline (selection, remember, picker) as the discovered
    transports. An optional ``name`` (a label that the user typed, or a mesh node name
    learned on an earlier connection) goes through to the DEVICE column of the picker.

    Args:
        host: The hostname or IP of the companion.
        port: The TCP port that it listens on.
        name: An optional name to show.

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

    pyserial does not enumerate a soldered platform-bus UART (for example ``/dev/ttyS1`` on
    the Luckfox Lyra), so a scan never finds it. Thus a configured serial ``port`` comes
    through this function into the same pipeline (selection, remember, picker) as the
    discovered transports. This function is the equivalent of :func:`tcp_device` for network
    companions. An optional ``name`` (the profile alias, or a mesh node name learned on an
    earlier connection) goes to the DEVICE column of the picker.

    Args:
        port: The serial port path (for example ``/dev/ttyS1`` or ``COM5``).
        name: An optional name to show.

    Returns:
        A serial :class:`DiscoveredDevice` ready to select or connect.
    """
    return DiscoveredDevice(
        port=port,
        description=name,
        product=name or None,
    )


def _sort_key(device: DiscoveredDevice) -> tuple[int, str]:
    """Sort probable companions first, then bridges, then unknown. The target id breaks ties."""
    return (_CONFIDENCE_RANK[device.confidence], device.target)


def discover_devices() -> list[DiscoveredDevice]:
    """Enumerate the serial ports that are attached to the system now.

    The probable LoRa companion devices (by known USB vendor ID) come first. If
    ``pyserial`` is not available, the function returns an empty list, and does not raise
    an error. Thus callers can use ``--port`` or ``--mock`` instead, with a friendly
    message.

    Returns:
        The discovered serial devices, probable candidates first, then sorted by port name.
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
    """Scan for nearby Bluetooth LE companions that advertise a ``MeshCore*`` name.

    The function uses ``bleak`` to listen for advertisements for ``timeout`` seconds. It
    keeps only the devices whose advertised name marks them as a MeshCore companion. If
    ``bleak`` is not available, or the platform has no usable Bluetooth adapter, the
    function returns an empty list, and does not raise an error. The reason: BLE is an
    optional transport, and if it is missing, it must never block serial discovery or the
    simulator.

    Args:
        timeout: The time, in seconds, to scan for devices that advertise.

    Returns:
        The discovered BLE companions (deduplicated by address), sorted by name.
    """
    global _ble_unavailable
    _ble_unavailable = None

    try:
        from bleak import BleakScanner
    except ImportError:  # bleak not installed → BLE is not available
        return []

    try:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except _BLE_NOT_AVAILABLE as exc:
        # A state that the user can repair: Bluetooth is off, or (on macOS, frequently) the
        # terminal that runs us does not have the Bluetooth permission. That is not the
        # "no adapter / driver hiccup" case of the broad catch below. When this code
        # returned [] silently, the user could not tell it from "nothing is nearby". The
        # one message that could tell the user what to do was lost at debug level, under a
        # log that has WARNING as its default. Keep the sentence that bleak wrote, because
        # it already names the remedy.
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
    """Watch for the advertisement of one known companion, and return its live ``BLEDevice``.

    This is the Bluetooth equivalent of
    :func:`~meshterm.core.connection.serial_port_present`. It answers the same question
    that the reconnect flow asks about a serial port: is the device back yet? A serial port
    has an OS registry to examine, but a peripheral has none. A peripheral exists only while
    it advertises, so the probe is a scan. ``find_device_by_address`` returns at the moment
    when the address is heard, not at the end of the time window. Thus a long ``timeout``
    costs nothing when the device is present, and the function continues to listen when it
    is not.

    The returned handle is as important as the answer. After a power cycle, a companion is a
    new peripheral for the OS. The ``BLEDevice`` from an earlier scan names a connection
    endpoint that does not resolve now. On Windows, if bleak gets that stale object, the
    connection fails immediately. When a newly scanned handle answers the presence question,
    the reconnection that follows opens the device that is really there (refer to
    :meth:`~meshterm.context.AppContext.reconnect`).

    Never raises. A missing ``bleak``, a disabled adapter, or a driver hiccup gives "not
    advertising". That is the true answer for a device that we cannot reach now.

    Args:
        address: The Bluetooth address to listen for (as discovery reports it).
        timeout: The time, in seconds, to listen before this round stops.

    Returns:
        The live ``bleak.BLEDevice`` if the address advertised in ``timeout``, else
        ``None``. Typed ``object``, so that ``bleak`` stays an optional dependency.
    """
    try:
        from bleak import BleakScanner
    except ImportError:  # bleak not installed → BLE is not available
        return None

    try:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - no adapter / OS Bluetooth off / driver hiccup
        _log.debug("BLE presence scan for %s unavailable: %s", address, exc)
        return None


async def discover_all(
    *, ble: bool = True, ble_timeout: float = BLE_SCAN_TIMEOUT_S
) -> list[DiscoveredDevice]:
    """Enumerate each companion that is attached or in range, across all transports.

    The serial enumeration is instant. The BLE scan is the part that takes ``ble_timeout``
    seconds. The serial devices come first in the list (they are already attached and
    connect fastest), then the BLE companions.

    Args:
        ble: Whether to include a Bluetooth LE scan (skip it to avoid the scan delay).
        ble_timeout: The time, in seconds, of the scan for BLE companions.

    Returns:
        All the discovered devices: serial (probable LoRa first), then BLE companions.
    """
    # ``comports()`` is a blocking SetupAPI/sysfs walk. It takes hundreds of milliseconds
    # on Windows. On a slow console, that is long enough to freeze the spinner that covers
    # this call. Run it off the event loop, so that the animation that shows the wait
    # continues to run.
    serial = await asyncio.to_thread(discover_devices)
    if not ble:
        return serial
    # A device that is paired over both USB and BLE almost never collides by stable_id (USB
    # serial number against BLE address). Thus no cross-transport dedup is necessary here.
    ble_devices = await discover_ble_devices(ble_timeout)
    return serial + ble_devices


def spi_device(wiring: object, *, name: str = "") -> DiscoveredDevice:
    """Build the :class:`DiscoveredDevice` for a radio on the SPI bus of the host itself.

    MeshTerm "finds" such a radio the same as a serial port: its ``/dev/spidev*`` device
    file exists. But nothing answers on it until MeshTerm starts a node. Thus MeshTerm
    lists it from its wiring (the wiring of a profile, or the defaults of the AIO), and
    does not probe it.

    Args:
        wiring: The :class:`~meshterm.core.config.SpiWiring` of the radio.
        name: An optional name to show (the description of a profile, or a node name).

    Returns:
        An SPI :class:`DiscoveredDevice` ready to select or connect.
    """
    return DiscoveredDevice(
        transport=TRANSPORT_SPI,
        port=wiring.spidev,  # type: ignore[attr-defined]
        spi=wiring,
        name=name or None,
        description=name or "SPI radio",
        product=name or None,
    )
