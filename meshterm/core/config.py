# SPDX-License-Identifier: Apache-2.0
"""Machine setup and named device profiles.

MeshTerm reads the config from a TOML file (by default ``~/.meshterm/config.toml``). CLI
flags can override it for one run. Profiles let you give a name to your hardware (``yagi``
repeater, ``local`` repeater, ``observer`` bot, ``s3`` serial companion), with a serial port
and defaults. Thus commands can address the hardware by name.

This file tells only where things are and which device to talk to. How MeshTerm itself
behaves (cooldowns, retry budgets, how much history it keeps, whether it connects at start)
is a **preference**. The preferences are in :mod:`meshterm.core.preferences`, and the user
edits them on the Preferences page, not in a text editor. The two were one thing until the
page existed. This is why some behaviour keys were in this file before, with no screen for
them.
"""

from __future__ import annotations

import os
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib


#: Points the config and the data of MeshTerm to a directory that is not the home
#: directory. For example: to try a build away from a real history, for a portable install
#: on a USB stick, and to run two devices from two directories. MeshTerm reads it each time
#: and does not cache it, so a test can change it between cases.
CONFIG_DIR_ENV = "MESHTERM_HOME"

#: The file name of the history in the config directory, when ``config.toml`` and ``--db``
#: do not change it.
DB_FILENAME = "meshterm.db"

#: The history of the simulator. It is next to the real history, and ``--mock`` runs use it
#: when they do not name a database themselves. ``--mock`` is a fake device, not a fake
#: MeshTerm: MeshTerm stores what the simulator advertises, the same as all that a real
#: companion says. Thus, without its own database, the four invented contacts of the
#: simulator show in the mesh walk, the dashboard, and the map of your real mesh.
MOCK_DB_FILENAME = "meshterm-mock.db"


def default_config_dir() -> Path:
    """Return the directory that MeshTerm uses for config and data.

    ``$MESHTERM_HOME`` has priority if it is set and not empty. Else the directory is
    ``.meshterm`` under the home directory of the OS (``%USERPROFILE%`` on Windows,
    ``$HOME`` on Unix), as :meth:`Path.home` reports it.

    The override exists because the database is the valuable part of an install: the
    history of all that the device has overheard. Before the override, there was no way to
    point a second copy of MeshTerm to a different database. A downloaded build opened the
    same 34MB file as the checkout from which it was built, with no warning.

    Returns:
        The resolved path of the config directory (it does not always exist).
    """
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".meshterm"


@dataclass(frozen=True, slots=True)
class SpiWiring:
    """How a LoRa chip on the SPI bus of the host is wired, for a ``transport = "spi"`` profile.

    The defaults are the wiring of the hackergadgets uConsole AIO v1. Thus that board does
    not need a ``[profiles.<name>.spi]`` table at all, and each other board gives only what
    is different. These values are facts about the board. This is why they are in
    ``config.toml`` with the profile, and not with the settings of the radio. The frequency,
    the bandwidth, and the power belong to the node: the node saves them, and you edit them
    on Device config, the same as for each companion.

    Attributes:
        bus_id: The SPI bus (``/dev/spidev<bus_id>.<cs_id>``).
        cs_id: The SPI chip-select device on that bus.
        cs_pin: A GPIO that MeshTerm drives by hand as the chip select, or ``-1`` for the
            chip select of the bus.
        gpio_chip: The ``/dev/gpiochip<n>`` of the pins below, or ``-1`` (the default) to
            find the controller of the Raspberry Pi header by its label: ``pinctrl-bcm2711``
            on a CM4, ``pinctrl-rp1`` on a CM5. MeshTerm does not trust a number, because
            the number is different on the two modules, and it changed between kernel
            releases on the CM5 (refer to :func:`~meshterm.core.spiradio.gpio_chip`). If you
            give a number, MeshTerm uses that number.
        use_gpiod_backend: Drive the pins through ``gpiod`` instead of ``python-periphery``.
        reset_pin: The reset line of the chip.
        busy_pin: The busy line of the chip.
        irq_pin: The interrupt line of the chip (DIO1).
        txen_pin: A transmit-enable line for an external RF switch, or ``-1``.
        rxen_pin: A receive-enable line for an external RF switch, or ``-1``.
        en_pins: The power-enable lines that MeshTerm raises before it touches the chip
            (the AIO v2 must have ``[27]``). Empty for none.
        leds: The switches that the kernel exposes as LEDs (``/sys/class/leds/<name>``),
            each one ``"name=brightness"``. MeshTerm sets them before it touches the chip,
            and puts them back as they were when the node ends. If the device tree of a
            board gives its power and pin-routing switches to ``gpio-leds``, MeshTerm
            drives the board through these switches. This is because that driver holds the
            lines, and the kernel refuses a GPIO request for them. The Cap LoRa-1262 of the
            Cardputer Zero uses two: ``ext_5v_out=1`` powers the header that the Cap is on,
            and ``ext_usb_gpio_fun=0`` keeps two pins of that header on GPIO, not on USB.
            At ``1``, the reset line of the Cap is cut, and the chip stays in reset. Empty
            for none.
        pi4io_bus: The I2C bus of a PI4IOE5V6408 expander with which the board switches the
            RF path of its radio, or ``-1`` (the default) for none.
        pi4io_address: The address of that expander: ``0x43`` when its ADDR pin is low,
            ``0x44`` when it is high.
        pi4io_high: The pins of the expander (0–7) that MeshTerm drives high before it
            touches the chip. Each other pin stays an input.
        use_dio2_rf: Whether DIO2 drives the RF switch.
        use_dio3_tcxo: Whether DIO3 powers a TCXO.
        is_waveshare: The wiring quirks of the Waveshare HAT, which the radio library
            knows.
        python: An interpreter to run the node, when the interpreter that MeshTerm finds
            is not the one that you want. Empty to let MeshTerm find one.
        gps_port: The serial port of a GPS receiver on the same board, or empty for none.
            The node runs it the same way that MeshCore firmware runs the GPS of a board:
            a ``gps`` switch and a ``gps_interval`` on Device config. The port belongs to
            the radio, so the device screen does not offer it as a companion. The Cap of
            the Cardputer Zero has one on ``/dev/serial0``.
        gps_baud: The line speed of the receiver. The NMEA default is 9600. The receiver
            of the Cap runs at 115200.
    """

    bus_id: int = 1
    cs_id: int = 0
    cs_pin: int = -1
    gpio_chip: int = -1
    use_gpiod_backend: bool = False
    reset_pin: int = 25
    busy_pin: int = 24
    irq_pin: int = 26
    txen_pin: int = -1
    rxen_pin: int = -1
    en_pins: tuple[int, ...] = ()
    leds: tuple[str, ...] = ()
    pi4io_bus: int = -1
    pi4io_address: int = 0x43
    pi4io_high: tuple[int, ...] = ()
    use_dio2_rf: bool = True
    use_dio3_tcxo: bool = True
    is_waveshare: bool = False
    python: str = ""
    gps_port: str = ""
    gps_baud: int = 9600

    #: The TOML keys that this table once accepted and must never accept again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED`).
    #: ``preamble_length`` was never wiring: it is protocol. A fixed 12, on a mesh that sends
    #: 32, made the node deaf to most traffic. Now the node derives it from the spreading
    #: factor, as MeshCore does.
    RETIRED: ClassVar[frozenset[str]] = frozenset({"preamble_length"})

    @classmethod
    def from_toml(cls, table: dict[str, Any]) -> SpiWiring:
        """Build the wiring from a ``[profiles.<name>.spi]`` table, with defaults for the rest.

        The function refuses a key that this dataclass does not know, and does not ignore
        it. If the function ignored it, a misspelled ``irq_pn = 22`` leaves the default in
        force, and the radio is deaf with no message that tells why.

        Raises:
            ValueError: For an unknown key, or for a value of the wrong type.
        """
        known = {f.name: f for f in cls.__dataclass_fields__.values()}
        retired = sorted(set(table) & cls.RETIRED)
        if retired:
            raise ValueError(
                f"{', '.join(retired)} is no longer a wiring key — the node sets the "
                "preamble from the spreading factor, as MeshCore does; delete the line"
            )
        unknown = sorted(set(table) - set(known))
        if unknown:
            raise ValueError(f"unknown SPI wiring key(s): {', '.join(unknown)}")
        hints = typing.get_type_hints(cls)
        values: dict[str, Any] = {}
        for key, raw in table.items():
            default = getattr(cls(), key)
            if isinstance(default, bool):
                ok = isinstance(raw, bool)
            elif isinstance(default, int):
                ok = isinstance(raw, int) and not isinstance(raw, bool)
            elif isinstance(default, tuple):
                item = typing.get_args(hints[key])[0]  # what the list holds: pins or switches
                ok = isinstance(raw, list) and all(
                    isinstance(p, item) and not isinstance(p, bool) for p in raw
                )
                raw = tuple(raw) if ok else raw
            else:
                ok = isinstance(raw, str)
            if not ok:
                raise ValueError(f"SPI wiring {key} = {raw!r} is not a {type(default).__name__}")
            values[key] = raw
        for entry in values.get("leds", ()):
            name, sep, brightness = entry.partition("=")
            if not (name and sep and brightness.isdigit()):
                raise ValueError(f"SPI wiring leds entry {entry!r} is not name=brightness")
        return cls(**values)

    @property
    def spidev(self) -> str:
        """The SPI device file that this wiring opens."""
        return f"/dev/spidev{self.bus_id}.{self.cs_id}"


@dataclass(slots=True)
class DeviceProfile:
    """Connection defaults for one physical companion.

    A profile addresses a serial companion (through ``port``), a Bluetooth companion
    (through ``transport = "ble"`` and ``address``), or a network companion (through
    ``transport = "tcp"``, ``host``, and ``tcp_port``). The default ``transport`` is serial,
    so the existing profiles with only a port do not change.

    Attributes:
        name: The profile alias (for example, ``"yagi"``).
        port: The serial port path (for example, ``COM5`` or ``/dev/ttyUSB0``). For serial
            profiles.
        baudrate: The serial baud rate.
        default_tx_power: The TX power to assume or restore for this device, if known.
        description: A free-text note about the hardware.
        transport: ``"serial"`` (the default), ``"ble"``, or ``"tcp"``.
        address: The Bluetooth address (for example, ``AA:BB:CC:DD:EE:FF``). For BLE
            profiles.
        ble_pin: An optional BLE pairing PIN, if the Bluetooth companion must have one.
        host: The host name or IP of a network companion (for example,
            ``"192.168.1.50"``). For TCP profiles.
        tcp_port: The TCP port on which the network companion listens. For TCP profiles
            (the default is :data:`~meshterm.core.discovery.DEFAULT_TCP_PORT` when a host
            is given without a port).
        spi: How the radio is wired, for ``transport = "spi"``: a LoRa chip on the SPI bus
            of the host itself, run by a node that MeshTerm starts itself
            (:mod:`meshterm.core.spiradio`).
    """

    name: str
    port: str | None = None
    baudrate: int = 115200
    default_tx_power: int | None = None
    description: str = ""
    transport: str = "serial"
    address: str | None = None
    ble_pin: str | None = None
    host: str | None = None
    tcp_port: int | None = None
    spi: SpiWiring | None = None

    @property
    def is_spi(self) -> bool:
        """Whether this profile addresses a radio on the SPI bus of the host itself."""
        return self.transport == "spi"

    @property
    def is_ble(self) -> bool:
        """Whether this profile addresses a Bluetooth LE companion."""
        return self.transport == "ble"

    @property
    def is_tcp(self) -> bool:
        """Whether this profile addresses a network (TCP) companion."""
        return self.transport == "tcp"

    @property
    def tcp_endpoint(self) -> str | None:
        """The ``host:port`` of a TCP profile, or ``None`` if it is not TCP or has no host."""
        if not self.is_tcp or not self.host:
            return None
        from .discovery import DEFAULT_TCP_PORT

        return f"{self.host}:{self.tcp_port or DEFAULT_TCP_PORT}"


@dataclass(slots=True)
class Settings:
    """The place where MeshTerm keeps its files, and the device to talk to.

    Attributes:
        config_dir: The directory that holds the config file and the database.
        db_path: The location of the SQLite database.
        default_profile: The profile to use when ``--profile`` is not given.
        profiles: The map of profile names to :class:`DeviceProfile`.
        connect_on_start: Whether the interactive session opens the connection to the
            companion (and starts to listen all the time in the background) immediately
            at start. When ``False``, the connection opens lazily: only when the user turns
            on monitoring, or when a tool first needs the device. Thus the start of the
            menu touches no serial port. This value is here, not with the preferences,
            because it is about which device this machine talks to and when, next to the
            profile that names that device. Also, it decides its own question before the
            session that shows a Preferences page exists.
    """

    config_dir: Path = field(default_factory=default_config_dir)
    db_path: Path | None = None
    default_profile: str | None = None
    profiles: dict[str, DeviceProfile] = field(default_factory=dict)
    connect_on_start: bool = True

    def __post_init__(self) -> None:
        """Derive the dependent paths that the caller did not give."""
        if self.db_path is None:
            self.db_path = self.config_dir / DB_FILENAME

    def resolve_profile(self, name: str | None) -> DeviceProfile | None:
        """Find a profile by name, or use the default profile.

        Args:
            name: The requested profile name, or ``None`` to use the default.

        Returns:
            The matching :class:`DeviceProfile`, or ``None`` if neither the requested
            profile nor the default profile is defined.
        """
        key = name or self.default_profile
        if key is None:
            return None
        return self.profiles.get(key)

    @classmethod
    def load(cls, config_path: Path | None = None) -> Settings:
        """Read the config from a TOML file, and return the defaults if the file is absent.

        Args:
            config_path: An explicit path to a config file. The default is
                ``<config_dir>/config.toml``.

        Returns:
            A populated :class:`Settings` instance.
        """
        config_dir = default_config_dir()
        path = config_path or (config_dir / "config.toml")
        data: dict[str, Any] = {}
        if path.exists():
            with path.open("rb") as fh:
                data = tomllib.load(fh)

        profiles: dict[str, DeviceProfile] = {}
        for pname, pdata in (data.get("profiles") or {}).items():
            # If the profile does not state the transport, find it from the endpoint that the
            # profile has: a ``host`` means TCP, an ``address`` means BLE, else serial. Thus the
            # profiles with only a port work without a change, and a bare ``host`` or
            # ``address`` is enough to declare a network or Bluetooth profile.
            transport = pdata.get("transport") or (
                "spi"
                if "spi" in pdata
                else "tcp"
                if pdata.get("host")
                else "ble"
                if pdata.get("address")
                else "serial"
            )
            tcp_port = pdata.get("tcp_port")
            spi = None
            if transport == "spi":
                try:
                    spi = SpiWiring.from_toml(pdata.get("spi") or {})
                except ValueError as exc:
                    raise ValueError(f"[profiles.{pname}.spi]: {exc}") from exc
            profiles[pname] = DeviceProfile(
                name=pname,
                port=pdata.get("port"),
                baudrate=pdata.get("baudrate", 115200),
                default_tx_power=pdata.get("default_tx_power"),
                description=pdata.get("description", ""),
                transport=transport,
                address=pdata.get("address"),
                ble_pin=pdata.get("ble_pin"),
                host=pdata.get("host"),
                tcp_port=int(tcp_port) if tcp_port is not None else None,
                spi=spi,
            )

        return cls(
            config_dir=config_dir,
            db_path=Path(data["db_path"]) if data.get("db_path") else None,
            default_profile=data.get("default_profile"),
            profiles=profiles,
            connect_on_start=bool(data.get("connect_on_start", True)),
        )
