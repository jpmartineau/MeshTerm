# SPDX-License-Identifier: Apache-2.0
"""A LoRa radio on the SPI bus of the host, with a node that MeshTerm runs only while it runs.

Some boards put the radio chip directly on the SPI bus of the host (we made this module
for the AIO of the uConsole). These boards have no microcontroller, thus no companion
firmware to talk to. A software node must run in some place. The standalone
``meshterm-spi-bridge`` service runs one all the time. This keeps the node on the mesh
while MeshTerm is closed, but the service holds the pins of the radio permanently. Thus no
other LoRa program can use them until the service stops.

This module has the other shape: **MeshTerm runs the node itself, for exactly as long as
it is connected.** :class:`SpiRadioDevice` starts the node
(:mod:`meshterm.core.radionode`) as a child process when it connects. It talks to the node
through the usual companion protocol on a loopback port. Thus each feature works exactly
as with a network companion. It stops the node when it disconnects. The pins become free
when MeshTerm quits, and also when it crashes. The node exits with its parent (refer to
:mod:`~meshterm.core.radionode`), and the kernel releases the GPIO lines and the SPI
handle of a process when the process exits.

**Why a child process and not a library call.** The radio library (``openhop-core``) is an
optional install. On purpose, not each desktop MeshTerm has it, and the one-file build
cannot import it. Thus the node runs with the Python that has the library
(:func:`find_runtime`). This is the MeshTerm Python when it was installed with the ``spi``
extra, the venv of the bridge, or the Python of the ``meshcore-uconsole`` package. A
separate process also keeps the radio timing of the node away from the event loop that
draws the screen.

**One radio, one identity.** The state of a node (its identity key, prefs, channels, and
contacts) is in ``<config_dir>/radio/<spidev>/``. The directory has the name of the SPI
device, not of a profile. Thus, if the user gets to the same radio from the picker one day
and from a profile the next day, it is still the same node. The first time that a radio is
used, MeshTerm copies the identity and the contacts of the bridge
(:func:`import_bridge_state`). Thus a node that exists already keeps its key, and with
the key its place in the contact lists of all the other nodes.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .config import DeviceProfile, SpiWiring
from .connection import DeviceCommandError, MeshCoreDevice
from .discovery import TRANSPORT_SERIAL, DiscoveredDevice, spi_device

_log = logging.getLogger(__name__)

#: The radio library that the node must have, as ``pip`` spells it. Each "install it" hint
#: gives this name.
RUNTIME_PACKAGE = "openhop-core[hardware]"

#: The time that a node gets to report ready when it starts. It imports the radio library,
#: starts the chip (with some retries for a busy pin), and starts to listen. On a board of
#: the PicoCalc class, the import alone takes several seconds.
READY_TIMEOUT_S = 60.0

#: The time that a node gets to shut down by itself after its stdin closes, before
#: MeshTerm sends SIGTERM. Then the same wait again before SIGKILL. The shutdown of the
#: node saves the contacts and releases the radio. The signals are for a node that does not
#: answer any more.
STOP_GRACE_S = 8.0
TERM_GRACE_S = 3.0

#: The seed values of the node, the first time that a radio is used and the node has no
#: prefs of its own: the MeshCore US/Canada preset, at the 22 dBm of the AIO. After this,
#: the saved settings of the node have priority. The user edits them on Device config, as
#: for all companions.
DEFAULT_SEED: dict[str, object] = {
    "frequency_hz": 910_525_000,
    "bandwidth_hz": 62_500,
    "spreading_factor": 7,
    "coding_rate": 5,
    "tx_power_dbm": 22,
}

#: The service name of the standalone bridge, and the directory in which it keeps its state.
BRIDGE_SERVICE = "meshterm-spi-bridge.service"
BRIDGE_NAME = "meshterm-spi-bridge"

#: The ``MESHCORE_*`` variables that the bridge reads, mapped to the node pref that each
#: one seeds.
_BRIDGE_RADIO_ENV = {
    "MESHCORE_FREQUENCY": "frequency_hz",
    "MESHCORE_BANDWIDTH": "bandwidth_hz",
    "MESHCORE_SPREADING_FACTOR": "spreading_factor",
    "MESHCORE_CODING_RATE": "coding_rate",
    "MESHCORE_TX_POWER": "tx_power_dbm",
}


class SpiRadioError(DeviceCommandError):
    """The node did not start. The message is for the person who must correct the problem.

    It is a :class:`~meshterm.core.connection.DeviceCommandError`, because the startup
    picker and the connect path pass that error through as a failure that the user can act
    on. They show it with its remedy, and do not change it into "didn't answer".

    Attributes:
        kind: The type of problem: ``unsupported``, ``runtime``, ``no-spi``, ``no-gpio``,
            ``permission``, ``busy``, or ``failed`` (refer to :func:`explain`).
    """

    def __init__(self, kind: str, message: str) -> None:
        """Keep ``message`` as the error text, and ``kind`` as its classification."""
        super().__init__(message)
        self.kind = kind


# --- the locations -------------------------------------------------------------------------


def node_script() -> Path:
    """The source file of the node, which a different interpreter runs by its path.

    It is next to this module in a checkout and in an install. The one-file build includes
    it as a data file at the same relative location (``packaging/meshterm.spec``).
    """
    return Path(__file__).with_name("radionode.py")


def state_dir(config_dir: Path, wiring: SpiWiring) -> Path:
    """Where the node on the SPI device of ``wiring`` keeps its identity, settings, and lists."""
    return config_dir / "radio" / Path(wiring.spidev).name


def _on_linux() -> bool:
    """Whether this is Linux, the only system on which MeshTerm can drive a radio on the SPI bus."""
    return sys.platform.startswith("linux")


def _xdg_data_home() -> Path:
    """``$XDG_DATA_HOME``, or ``~/.local/share`` if it is not set."""
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".local" / "share"


def spi_present(wiring: SpiWiring) -> bool:
    """Whether the SPI device file of ``wiring`` exists on this machine (Linux only)."""
    return _on_linux() and os.path.exists(wiring.spidev)


#: The labels that the kernel gives to the GPIO controller behind the 40-pin header of a
#: Raspberry Pi, in search order: the RP1 of a Pi 5 or CM5, then the BCM2711 of a Pi 4 or
#: CM4, then the BCM2835 family of the older boards. The number of the controller is not a
#: fact about the board. It is chip 0 on a CM4, and chip 4 on a CM5 with the kernels that
#: came with it. It is chip 0 again after Raspberry Pi made an alias for the RP1 there
#: (raspberrypi/linux#6144). On some later kernels it is yet a different number, because
#: the probe order moved it. The label does not change.
HEADER_GPIO_LABELS = ("pinctrl-rp1", "pinctrl-bcm2711", "pinctrl-bcm2835")

#: ``GPIO_GET_CHIPINFO_IOCTL``: ``_IOR(0xB4, 0x01, struct gpiochip_info)``, which fills a
#: 68-byte ``{char name[32]; char label[32]; u32 lines}`` for the chip on which the fd is
#: open.
_GPIO_GET_CHIPINFO_IOCTL = 0x8044B401


def gpio_chip_labels(dev: Path = Path("/dev")) -> dict[int, str]:
    """The label of each ``/dev/gpiochip<n>`` as the kernel reports it, by ``n``.

    The function asks the GPIO character device itself. For this, no installed package is
    necessary, only the read access that the pins of the radio also use. The result is
    empty on a system that is not Linux. If a chip cannot be opened (no ``gpio`` group
    yet), the function does not include it. The start of the radio reports that problem.
    """
    try:
        import fcntl
    except ImportError:  # not a POSIX system: no GPIO character devices to ask
        return {}
    labels: dict[int, str] = {}
    for path in dev.glob("gpiochip[0-9]*"):
        try:
            number = int(path.name.removeprefix("gpiochip"))
            fd = os.open(path, os.O_RDONLY)
        except (ValueError, OSError):
            continue
        try:
            info = bytearray(68)
            fcntl.ioctl(fd, _GPIO_GET_CHIPINFO_IOCTL, info)
        except OSError:
            continue
        finally:
            os.close(fd)
        labels[number] = bytes(info[32:64]).split(b"\0", 1)[0].decode("ascii", "replace")
    return labels


def header_gpio_chip(labels: Mapping[int, str]) -> int | None:
    """The chip that has the 40-pin header of a Pi, by its label (:data:`HEADER_GPIO_LABELS`)."""
    for wanted in HEADER_GPIO_LABELS:
        for number in sorted(labels):
            if labels[number] == wanted:
                return number
    return None


def gpio_chip(wiring: SpiWiring) -> int:
    """The ``/dev/gpiochip<n>`` that the pins of the wiring are on.

    If the profile names a ``gpio_chip``, the function uses it. If not, it uses the
    controller of the Raspberry Pi header, found by its label. Thus the CM4 and the CM5 of
    a uConsole both work with the defaults of the AIO, whatever number the kernel gave to
    the chip at this boot. If no controller has a header label, the function uses chip 0.
    This was the meaning of the default before the function looked for the label.
    """
    if wiring.gpio_chip >= 0:
        return wiring.gpio_chip
    found = header_gpio_chip(gpio_chip_labels())
    return 0 if found is None else found


#: The M5Stack Cap LoRa-1262 on the 14-pin EXT header of a Cardputer Zero. We confirmed it
#: on the hardware, and with the factory test of M5 (which names the same three lines). The
#: SX1262 is on the second chip select of SPI0, and the display uses the first chip select.
#: The header of the Cap has no power until the ``ext_5v_out`` switch of the device tree is
#: on. Two of its pins (reset, interrupt) get to the chip only while ``ext_usb_gpio_fun``
#: routes them to GPIO, not to USB. Its PI4IOE5V6408 at 0x43 on I2C 1 turns on the RF path
#: from pin P0. The Cardputer variant of Meshtastic drives that pin in the same way. DIO2
#: switches the antenna and DIO3 supplies a 1.8 V TCXO, as on the AIO. Its GPS talks NMEA
#: at 115200 on the UART of the header, which is the ``serial0`` of the Pi (``/dev/ttyS0``
#: on the Zero). The GPS gets its power from ``ext_5v_out``, as the radio does. Thus it
#: talks only while the node runs.
CARDPUTER_ZERO_CAP = SpiWiring(
    bus_id=0,
    cs_id=1,
    reset_pin=26,
    busy_pin=22,
    irq_pin=23,
    leds=("ext_5v_out=1", "ext_usb_gpio_fun=0"),
    pi4io_bus=1,
    pi4io_address=0x43,
    pi4io_high=(0,),
    gps_port="/dev/serial0",
    gps_baud=115200,
)


#: The locations where Linux shows the device tree that the machine booted with, and its
#: USB devices.
DEVICE_TREE = Path("/sys/firmware/devicetree/base")
USB_DEVICES = Path("/sys/bus/usb/devices")


@functools.cache
def device_tree_compatibles(root: Path = DEVICE_TREE) -> frozenset[str]:
    """All the ``compatible`` strings in the booted device tree, or empty if there is no tree.

    The function reads them one time in each run, because the tree does not change after
    boot, and the device screen asks at each pass.
    """
    found: set[str] = set()
    try:
        for path in root.rglob("compatible"):
            try:
                found.update(
                    v.decode("ascii", "replace") for v in path.read_bytes().split(b"\0") if v
                )
            except OSError:
                continue
    except OSError:
        pass
    return frozenset(found)


def usb_devices(root: Path = USB_DEVICES) -> frozenset[tuple[str, str, str, str]]:
    """All the USB devices attached now, as ``(vendor id, product id, maker, product)``.

    The ids are the lower-case hex that sysfs gives. A device that names no maker or
    product has ``""`` in that field. Empty if there is no sysfs.
    """

    def read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    found: set[tuple[str, str, str, str]] = set()
    for device in root.glob("*"):
        vendor = read(device / "idVendor")
        if vendor:
            found.add(
                (
                    vendor,
                    read(device / "idProduct"),
                    read(device / "manufacturer"),
                    read(device / "product"),
                )
            )
    return frozenset(found)


#: What the uConsole overlay of ClockworkPi puts in the device tree: the 5-inch display of
#: the uConsole (the display of the DevTerm is ``cw,cwd686``), its AXP228 power chip, and
#: its backlight. We found these on the uConsole of JP, a CM5. The match uses the
#: compatible strings, never the paths, because the paths change with the Compute Module.
UCONSOLE_TREE = frozenset({"cw,cwu50", "x-powers,axp228", "ocp8178-backlight"})

#: The built-in keyboard of the uConsole as USB reports it. 1eaf:0024 alone is a general
#: hobby-board id (the Maple of LeafLabs). Thus its maker and product names are part of the
#: sign.
UCONSOLE_KEYBOARD = ("1eaf", "0024", "ClockworkPI", "uConsole")


def is_uconsole() -> bool:
    """Whether this machine is certainly a ClockworkPi uConsole.

    A uConsole runs a stock Compute Module, and its model string tells only that. Thus one
    fact is not sufficient: all the signs must be there, from two independent sources. The
    device-tree signs come from the overlay of ClockworkPi, which describes the hardware
    (:data:`UCONSOLE_TREE`). The keyboard is the hardware that answers for itself
    (:data:`UCONSOLE_KEYBOARD`). An overlay applied to the wrong machine has no uConsole
    keyboard, and a uConsole keyboard connected to a different machine has no uConsole
    overlay.
    """
    return UCONSOLE_TREE <= device_tree_compatibles() and UCONSOLE_KEYBOARD in usb_devices()


@dataclass(frozen=True)
class BuiltinRadio:
    """A radio whose wiring comes with MeshTerm, listed without a profile on its board.

    Attributes:
        name: The name that the device screen gives it, and that its node reports as its
            model. Empty for the plain "SPI radio".
        wiring: How it is wired.
        marker: A path that only its board has. It is for a radio whose ``/dev/spidev*``
            file alone matches each Raspberry Pi with SPI switched on. Empty for no
            marker.
        board: A test that only its machine passes, for a machine that has no single path
            of its own (:func:`is_uconsole`). ``None`` for no test.
    """

    name: str
    wiring: SpiWiring
    marker: str = ""
    board: Callable[[], bool] | None = None

    def present(self) -> bool:
        """Whether this machine is its board: the SPI device file and all its signs are there."""
        return (
            spi_present(self.wiring)
            and (not self.marker or os.path.exists(self.marker))
            and (self.board is None or self.board())
        )


#: The radios whose wiring MeshTerm knows, in list order. On a device file, MeshTerm uses
#: the first radio that is present. A profile on the same device file has priority over all
#: of them. The AIO has its name only where the machine is certainly a uConsole. On all
#: other boards with ``spidev1.0``, it is in the list with no name, because its default
#: wiring is still the best guess there.
BUILTIN_RADIOS = (
    BuiltinRadio("uConsole AIO", SpiWiring(), board=is_uconsole),
    BuiltinRadio("", SpiWiring()),
    BuiltinRadio("Cap LoRa-1262", CARDPUTER_ZERO_CAP, marker="/sys/class/leds/ext_5v_out"),
)


def board_name(wiring: SpiWiring) -> str:
    """The name of the shipped board on which the radio of ``wiring`` is, or ``""`` for none.

    The node reports this name as its model. Thus the Cap shows as "Cap LoRa-1262", and the
    AIO as "uConsole AIO", in all places that show the model of a device. The match uses
    the SPI device file on the board of this machine, not the full wiring. Thus a profile
    that changes the pins of the Cap is still the Cap.
    """
    for radio in BUILTIN_RADIOS:
        if radio.name and radio.wiring.spidev == wiring.spidev and radio.present():
            return radio.name
    return ""


def builtin_wiring(spidev: str) -> SpiWiring | None:
    """The shipped wiring for the radio on ``spidev``, when this machine is its board."""
    for radio in BUILTIN_RADIOS:
        if radio.wiring.spidev == spidev and radio.present():
            return radio.wiring
    return None


def spi_radios(
    profiles: Mapping[str, DeviceProfile] | None, listed: Iterable[DiscoveredDevice] = ()
) -> list[DiscoveredDevice]:
    """The radios on the SPI bus of this machine, as devices: the only answer to "which are here".

    There is one for each ``transport = "spi"`` profile whose ``/dev/spidev*`` file exists,
    named by the profile. Also, if no profile covers its device file, there is one for each
    :data:`BUILTIN_RADIOS` entry whose board is this machine: the uConsole AIO when its
    device file exists, and the Cap of the Cardputer Zero on a Cardputer Zero. The function
    probes nothing, because a radio on the bus has no node that runs until the user chooses
    one. Thus a radio is in the list exactly when there is a device file to open. The
    device screen and ``meshterm devices`` both use this list. Thus the two can never
    disagree about what is attached.

    Args:
        profiles: The configured profiles, or ``None`` when none are loaded.
        listed: The devices that were found already. A radio in them is not listed two
            times.

    Returns:
        One SPI :class:`~meshterm.core.discovery.DiscoveredDevice` for each radio that is
        not in ``listed``.
    """
    seen = {d.stable_id for d in listed}
    radios: list[DiscoveredDevice] = []
    for wiring, name in _present_wirings(profiles):
        device = spi_device(wiring, name=name)
        if device.stable_id in seen:
            continue
        seen.add(device.stable_id)
        radios.append(device)
    return radios


def _present_wirings(
    profiles: Mapping[str, DeviceProfile] | None,
) -> list[tuple[SpiWiring, str]]:
    """Each radio on the SPI bus of this machine as ``(wiring, name)``: profiles, then boards."""
    wirings = [(p.spi or SpiWiring(), p.name) for p in (profiles or {}).values() if p.is_spi]
    wirings = [(w, name) for w, name in wirings if spi_present(w)]
    covered = {wiring.spidev for wiring, _name in wirings}
    for builtin in BUILTIN_RADIOS:
        if builtin.wiring.spidev not in covered and builtin.present():
            wirings.append((builtin.wiring, builtin.name))
            covered.add(builtin.wiring.spidev)
    return wirings


def without_radio_ports(
    devices: Iterable[DiscoveredDevice], profiles: Mapping[str, DeviceProfile] | None
) -> list[DiscoveredDevice]:
    """``devices`` without the serial ports that a radio on the SPI bus owns: its board's GPS.

    The GPS of a board answers a port scan as all serial devices do. It has nothing that
    names it (no USB identity, thus pyserial calls it ``n/a``). For this reason, the list
    once showed it next to the radio as a companion to connect to. On a Cardputer Zero
    with its Cap, it had its own row above or below the Cap. When nothing was remembered,
    it was the one port that a command-line connect selected. The node of the radio reads
    this port. Thus, where that radio is in the list, the port is not. The match uses the
    real path of the port, because a wiring names the stable alias (``/dev/serial0``), and
    the scan names the device that the alias points to (``/dev/ttyS0``).
    """
    owned = {
        os.path.realpath(wiring.gps_port)
        for wiring, _name in _present_wirings(profiles)
        if wiring.gps_port
    }
    return [
        d
        for d in devices
        if not (d.transport == TRANSPORT_SERIAL and d.port and os.path.realpath(d.port) in owned)
    ]


# --- find a Python with the radio library --------------------------------------------------

#: Each interpreter probed in this session: path -> whether it can import the radio library.
_probed: dict[str, bool] = {}


def _runtime_candidates(wiring: SpiWiring) -> list[str]:
    """The interpreters to probe for the radio library, the most specific first."""
    candidates: list[str] = []
    if wiring.python:
        candidates.append(os.path.expanduser(wiring.python))
    if not getattr(sys, "frozen", False):
        candidates.append(sys.executable)  # the MeshTerm Python, with the ``spi`` extra
    data = _xdg_data_home()
    candidates += [
        str(data / BRIDGE_NAME / "venv" / "bin" / "python"),
        "/opt/venvs/meshcore-uconsole/bin/python",
    ]
    system = shutil.which("python3")
    if system:
        candidates.append(system)
    seen: set[str] = set()
    return [c for c in candidates if c and not (c in seen or seen.add(c))]


def _can_import_runtime(python: str) -> bool:
    """Whether ``python`` can import ``openhop_core`` (one short subprocess, then cached)."""
    if python in _probed:
        return _probed[python]
    ok = False
    if os.path.exists(python):
        try:
            result = subprocess.run(
                [python, "-c", "import openhop_core"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_env(),
                timeout=30,
                check=False,
            )
            ok = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    _probed[python] = ok
    return ok


def find_runtime(wiring: SpiWiring) -> str:
    """An interpreter that can run the node, or raise :class:`SpiRadioError` (``runtime``).

    When the wiring names a ``python``, that is the only candidate. The name is a decision.
    If the function uses a different interpreter and tells nothing, it hides that the named
    interpreter does not work.
    """
    candidates = _runtime_candidates(wiring)
    if wiring.python:
        candidates = candidates[:1]
    for python in candidates:
        if _can_import_runtime(python):
            return python
    if wiring.python:
        raise SpiRadioError("runtime", f"{wiring.python} cannot import openhop_core")
    raise SpiRadioError("runtime", "no Python on this machine has the radio library")


def child_env() -> dict[str, str]:
    """The environment for a program that MeshTerm starts, without what a frozen build added.

    The bootloader of the one-file build leaves its own variables (``_PYI_*``,
    ``_MEIPASS2``), and points ``LD_LIBRARY_PATH`` at its unpacked libraries. If a
    different Python starts with these variables, it loads the copies of libssl and the
    related libraries that MeshTerm bundles, instead of its own. PyInstaller keeps the
    original value as ``LD_LIBRARY_PATH_ORIG``, and this function puts it back.
    """
    env = {k: v for k, v in os.environ.items() if not (k.startswith("_PYI") or k == "_MEIPASS2")}
    if getattr(sys, "frozen", False):
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is not None:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


# --- other holders of the radio ------------------------------------------------------------


def radio_holder() -> str | None:
    """Name the program that holds the radio already, when we know how to stop it.

    MeshTerm asks this before it starts the node. The answer of the node itself to a held
    pin is only "busy", after several seconds of retries. The two usual holders each have a
    one-line correction, and it is better to give that instead.
    """
    if not _on_linux():
        return None
    systemctl = shutil.which("systemctl")
    if systemctl:
        try:
            active = subprocess.run(
                [systemctl, "--user", "is-active", "--quiet", BRIDGE_SERVICE],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if active.returncode == 0:
                return "bridge"
        except (OSError, subprocess.SubprocessError):
            pass
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        if any(os.path.basename(a) == b"meshcore-console" for a in argv[:3]):
            return "console"
    return None


def explain(kind: str, detail: str, wiring: SpiWiring, holder: str | None = None) -> str:
    """The sentence that reports a failed start: what is wrong, and what to do about it."""
    if kind == "busy" or holder:
        if holder == "bridge":
            return (
                f"the {BRIDGE_NAME} service has the radio — stop it with "
                f"`systemctl --user stop {BRIDGE_NAME}` (and `disable` it to keep it stopped)"
            )
        if holder == "console":
            return "meshcore-console has the radio open — close it and connect again"
        return (
            "another program has the radio's pins "
            f"(`sudo lsof /dev/gpiochip{gpio_chip(wiring)}` shows which)"
        )
    if kind == "runtime":
        return (
            f"{detail}. Install it with `pipx inject mesh-term '{RUNTIME_PACKAGE}'`, or into a "
            "venv of its own and name that venv's python as `python` in the profile's spi table"
        )
    if kind == "no-spi":
        return (
            f"{wiring.spidev} does not exist — enable SPI (on a uConsole, add "
            "`dtoverlay=spi1-1cs` to /boot/firmware/config.txt) and reboot"
        )
    if kind == "no-gpio":
        return f"/dev/gpiochip{gpio_chip(wiring)} does not exist — check `gpio_chip` in the profile"
    if kind == "no-i2c":
        return (
            f"/dev/i2c-{wiring.pi4io_bus} does not exist — enable I2C "
            "(`dtparam=i2c_arm=on` in /boot/firmware/config.txt) and reboot"
        )
    if kind == "absent":
        return f"{detail} — is the radio attached, and seated the right way round?"
    if kind == "permission":
        groups = "spi,gpio,i2c" if wiring.pi4io_bus >= 0 else "spi,gpio"
        return (
            f"{detail} — add yourself to the {groups.replace(',', ', ')} groups "
            f"(`sudo usermod -aG {groups} $USER`) and log in again"
        )
    if kind == "unsupported":
        return detail
    return f"the radio node failed: {detail}"


# --- first use: copy the node of the bridge ------------------------------------------------


def _bridge_radio_seed() -> dict[str, int]:
    """The radio settings with which the bridge service runs, from the environment of its unit.

    The bridge takes its frequency, preset, and power from ``MESHCORE_*`` variables, which
    are usually set in a drop-in. A node that gets the identity of the bridge must start
    with the same radio settings, not with the default preset. Empty when there is no such
    service.
    """
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {}
    try:
        shown = subprocess.run(
            [systemctl, "--user", "show", "--property=Environment", BRIDGE_SERVICE],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    seed: dict[str, int] = {}
    for word in shown.stdout.removeprefix("Environment=").split():
        name, _, value = word.partition("=")
        if name in _BRIDGE_RADIO_ENV:
            try:
                seed[_BRIDGE_RADIO_ENV[name]] = int(value)
            except ValueError:
                continue
    return seed


def import_bridge_state(state: Path) -> list[str]:
    """Copy the node of the bridge into the state directory of a radio, at its first use.

    The function runs only while the state directory has no identity yet. It copies and
    does not move, thus the bridge still works for a user who goes back to it. The function
    looks for the identity where the bridge itself looks, in the same order: first the key
    of the ``meshcore-uconsole`` GUI (the bridge shares it), then the key of the bridge. The
    contacts come from the snapshot of the bridge. Its records are the contact dicts of the
    radio library itself, thus the node reads them with no change. The radio settings come
    from the environment of the service, and the node keeps the name of the bridge.

    Args:
        state: The state directory of the radio.

    Returns:
        What the function copied, for the log. Empty when it copied nothing.
    """
    if (state / "identity.key").exists():
        return []
    data = _xdg_data_home()
    bridge = data / BRIDGE_NAME
    carried: list[str] = []
    for key in (data / "meshcore-uconsole" / "identity.key", bridge / "identity.key"):
        if key.is_file():
            state.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(key, state / "identity.key")
            os.chmod(state / "identity.key", 0o600)
            carried.append(f"identity from {key}")
            break
    if not carried:
        return []
    contacts = bridge / "contacts.json"
    if contacts.is_file() and not (state / "contacts.json").exists():
        shutil.copyfile(contacts, state / "contacts.json")
        carried.append(f"contacts from {contacts}")
    if not (state / "prefs.json").exists():
        prefs: dict[str, object] = {"node_name": "uConsole", **_bridge_radio_seed()}
        (state / "prefs.json").write_text(json.dumps(prefs, indent=2), encoding="utf-8")
        carried.append("the bridge's name and radio settings")
    return carried


# --- the node process ----------------------------------------------------------------------


@dataclass
class NodeProcess:
    """One running node: the child process, and the loopback port on which it serves.

    Attributes:
        process: The child process.
        port: The loopback TCP port on which its frame server listens.
        public_key: The public key of the node, in hex, as the node reported it.
        log_path: The file that gets its stderr.
    """

    process: asyncio.subprocess.Process
    port: int
    public_key: str
    log_path: Path

    @property
    def alive(self) -> bool:
        """Whether the child process still runs."""
        return self.process.returncode is None

    async def stop(self) -> None:
        """Stop the node and wait for it: close its stdin, then SIGTERM, then SIGKILL.

        The close of stdin is the polite request: the node saves its contacts and releases
        the radio before it exits. The signals are for a node that does not listen any more.
        Each wait has a limit, thus a child process that is stuck can never delay a quit.
        """
        proc = self.process
        if proc.returncode is not None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
        for grace, escalate in ((STOP_GRACE_S, proc.terminate), (TERM_GRACE_S, proc.kill)):
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
                return
            except asyncio.TimeoutError:
                try:
                    escalate()
                except ProcessLookupError:
                    return
        await proc.wait()


def _rotate(log_path: Path) -> None:
    """Keep the node log of the previous run next to the log of this run, and no more logs."""
    if log_path.exists():
        try:
            log_path.replace(log_path.with_suffix(".log.1"))
        except OSError:
            pass


async def start_node(wiring: SpiWiring, state: Path, *, node_name: str) -> NodeProcess:
    """Start the node on the radio of ``wiring``, and wait until it listens.

    Args:
        wiring: How the radio is wired.
        state: The state directory of the radio.
        node_name: The name that a new node sends in its adverts until it gets a new name.

    Returns:
        The running node.

    Raises:
        SpiRadioError: If it cannot be started, with the reason already in words for the
            user.
    """
    if not _on_linux():
        raise SpiRadioError("unsupported", "a radio on the SPI bus needs Linux")
    # The chip that the pins are on, found by its label if the profile names no chip. The
    # function finds it one time here. Thus the node, each error message, and the `lsof`
    # hint all name the same chip.
    wiring = replace(wiring, gpio_chip=await asyncio.to_thread(gpio_chip, wiring))
    # Each of these runs one or two short subprocesses. None of them must run on the event
    # loop that draws the "connecting" card.
    holder = await asyncio.to_thread(radio_holder)
    if holder:
        raise SpiRadioError("busy", explain("busy", "", wiring, holder))
    try:
        python = await asyncio.to_thread(find_runtime, wiring)
    except SpiRadioError as exc:
        raise SpiRadioError(exc.kind, explain(exc.kind, str(exc), wiring)) from None

    state.mkdir(parents=True, exist_ok=True)
    for line in await asyncio.to_thread(import_bridge_state, state):
        _log.info("spi radio: carried over %s", line)
    config = {
        "state_dir": str(state),
        "wiring": {
            "bus_id": wiring.bus_id,
            "cs_id": wiring.cs_id,
            "cs_pin": wiring.cs_pin,
            "gpio_chip": wiring.gpio_chip,
            "use_gpiod_backend": wiring.use_gpiod_backend,
            "reset_pin": wiring.reset_pin,
            "busy_pin": wiring.busy_pin,
            "irq_pin": wiring.irq_pin,
            "txen_pin": wiring.txen_pin,
            "rxen_pin": wiring.rxen_pin,
            "en_pins": list(wiring.en_pins) or None,
            "leds": list(wiring.leds),
            "pi4io_bus": wiring.pi4io_bus,
            "pi4io_address": wiring.pi4io_address,
            "pi4io_high": list(wiring.pi4io_high),
            "use_dio2_rf": wiring.use_dio2_rf,
            "use_dio3_tcxo": wiring.use_dio3_tcxo,
            "is_waveshare": wiring.is_waveshare,
            "gps_port": wiring.gps_port,
            "gps_baud": wiring.gps_baud,
        },
        "seed": {"node_name": node_name, **DEFAULT_SEED},
        "model": board_name(wiring),
    }
    log_path = state / "node.log"
    _rotate(log_path)
    _log.info("spi radio: starting the node under %s (log %s)", python, log_path)
    with log_path.open("wb") as log_file:
        proc = await asyncio.create_subprocess_exec(
            python,
            str(node_script()),
            "--config",
            json.dumps(config),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=log_file,
            env=child_env(),
            start_new_session=True,  # MeshTerm handles ^C in the terminal, not the node
        )
    node = NodeProcess(process=proc, port=0, public_key="", log_path=log_path)
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=READY_TIMEOUT_S)
    except asyncio.CancelledError:
        proc.kill()  # cancelled during start (a probe deadline): nothing must keep the radio
        raise
    except asyncio.TimeoutError:
        await node.stop()
        detail = f"it did not start within {READY_TIMEOUT_S:.0f}s (see {log_path})"
        raise SpiRadioError("failed", explain("failed", detail, wiring)) from None
    try:
        status = json.loads(line) if line else {}
    except ValueError:
        status = {}
    if status.get("event") != "ready":
        await node.stop()
        kind = str(status.get("kind") or "failed")
        detail = str(status.get("message") or f"it exited without a word (see {log_path})")
        holder = await asyncio.to_thread(radio_holder) if kind == "busy" else None
        raise SpiRadioError(kind, explain(kind, detail, wiring, holder))
    node.port = int(status["port"])
    node.public_key = str(status.get("public_key") or "")
    _log.info("spi radio: node %s… ready on 127.0.0.1:%d", node.public_key[:16], node.port)
    return node


# --- the device ----------------------------------------------------------------------------


class SpiRadioDevice(MeshCoreDevice):
    """A companion connection to a node that MeshTerm starts on the radio of the host.

    Below the surface, it is exactly a network companion on ``127.0.0.1``. Each command,
    event, and trace goes through the TCP path of :class:`MeshCoreDevice` with no change.
    The life of the node is the same as the life of the connection: :meth:`connect` starts
    the node, and :meth:`disconnect` stops it. A reconnect (which disconnects and builds a
    new device) starts a new node.
    """

    def __init__(self, wiring: SpiWiring, state: Path, *, node_name: str = "MeshTerm") -> None:
        """Prepare the device. Nothing starts until :meth:`connect`.

        Args:
            wiring: How the radio is wired.
            state: The state directory of the radio (refer to :func:`state_dir`).
            node_name: The name that a new node sends in its adverts until it gets a new
                name.
        """
        super().__init__(transport="tcp", host="127.0.0.1", tcp_port=0)
        self._wiring = wiring
        self._state = state
        self._node_name = node_name
        self._node: NodeProcess | None = None

    @property
    def transport(self) -> str:
        """``"spi"``: the loopback socket below is a detail of the implementation."""
        return "spi"

    @property
    def endpoint(self) -> str:
        """The SPI device that the radio is on. MeshTerm uses it to name and remember the radio."""
        return self._wiring.spidev

    @property
    def wiring(self) -> SpiWiring:
        """How the radio that this device drives is wired."""
        return self._wiring

    async def connect(self) -> None:  # noqa: D102 - inherited docstring
        if self._mc is not None:
            return
        if self._node is None or not self._node.alive:
            self._node = await start_node(self._wiring, self._state, node_name=self._node_name)
            self._tcp_port = self._node.port
        try:
            await super().connect()
        except BaseException:
            await self._stop_node()
            raise

    async def link_present(self) -> bool:  # noqa: D102 - inherited docstring
        if self._node is not None and not self._node.alive:
            return False  # the node died, thus the radio is gone, whatever the socket says
        return await super().link_present()

    async def disconnect(self) -> None:  # noqa: D102 - inherited docstring
        try:
            await super().disconnect()
        finally:
            await self._stop_node()

    async def _stop_node(self) -> None:
        """Stop the node, if one runs, and forget it."""
        node, self._node = self._node, None
        if node is not None:
            await node.stop()

    def _no_response_message(self) -> str:
        """The node started but did not answer as a companion: point to its log."""
        where = self._node.log_path if self._node is not None else self._state / "node.log"
        return f"the radio node on {self._wiring.spidev} did not answer; its log is {where}"
