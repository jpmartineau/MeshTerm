# SPDX-License-Identifier: Apache-2.0
"""A LoRa radio on the host's own SPI bus, driven by a node MeshTerm runs only while it runs.

Some boards — the uConsole's AIO is the one this was built for — put the radio chip straight
on the host's SPI bus, with no microcontroller and so no companion firmware to talk to. A
software node has to run somewhere. The standalone ``meshterm-spi-bridge`` service runs one
all the time, which keeps the node on the mesh while MeshTerm is closed but holds the radio's
pins for good, so no other LoRa program can use them until the service is stopped.

This is the other shape: **MeshTerm runs the node itself, for exactly as long as it is
connected.** :class:`SpiRadioDevice` starts the node (:mod:`meshterm.core.radionode`) as a
child process when it connects, talks to it over the ordinary companion protocol on a
loopback port — so every feature works exactly as it does against a network companion — and
ends it when it disconnects. The pins go back when MeshTerm quits, and also when it crashes:
the node exits with its parent (see :mod:`~meshterm.core.radionode`), and the kernel frees a
process's GPIO lines and SPI handle when it exits.

**Why a child process and not a library call.** The radio library (``openhop-core``) is an
optional install, deliberately not something every desktop MeshTerm carries, and the
one-file build can't import it at all. So the node runs under whichever Python *has* the
library (:func:`find_runtime`): MeshTerm's own when it was installed with the ``spi``
extra, the bridge's venv, or the ``meshcore-uconsole`` package's. A separate process also
keeps the node's radio timing off the event loop that draws the screen.

**One radio, one identity.** A node's state — its identity key, preferences, channels and
contacts — lives under ``<config_dir>/radio/<spidev>/``, keyed by the SPI device rather than
by a profile name, so reaching the same radio from the picker one day and from a profile the
next is still the same node. The first time a radio is used, the bridge's identity and
contacts are copied in (:func:`import_bridge_state`) so an existing node keeps its key, and
with it its place in everyone else's contact list.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import SpiWiring
from .connection import DeviceCommandError, MeshCoreDevice

_log = logging.getLogger(__name__)

#: The radio library the node needs, as ``pip`` spells it — what every "install it" hint says.
RUNTIME_PACKAGE = "openhop-core[hardware]"

#: How long a starting node gets to report ready. It imports the radio library, brings the
#: chip up (retrying a busy pin a few times) and starts listening; on a PicoCalc-class board
#: the import alone is several seconds.
READY_TIMEOUT_S = 60.0

#: How long a node gets to shut down on its own after its stdin closes, before it is sent
#: SIGTERM, and then again before SIGKILL. Its own shutdown saves contacts and frees the
#: radio; the signals are for a node that has stopped answering.
STOP_GRACE_S = 8.0
TERM_GRACE_S = 3.0

#: What the node is seeded with the first time a radio is used and it has no preferences of
#: its own: MeshCore's US/Canada preset at the AIO's 22 dBm. Afterwards the node's saved
#: settings rule, edited on Device config like any companion's.
DEFAULT_SEED: dict[str, object] = {
    "frequency_hz": 910_525_000,
    "bandwidth_hz": 62_500,
    "spreading_factor": 7,
    "coding_rate": 5,
    "tx_power_dbm": 22,
}

#: The standalone bridge's service name and the directory it keeps its state in.
BRIDGE_SERVICE = "meshterm-spi-bridge.service"
BRIDGE_NAME = "meshterm-spi-bridge"

#: ``MESHCORE_*`` variables the bridge reads, mapped to the node preference each seeds.
_BRIDGE_RADIO_ENV = {
    "MESHCORE_FREQUENCY": "frequency_hz",
    "MESHCORE_BANDWIDTH": "bandwidth_hz",
    "MESHCORE_SPREADING_FACTOR": "spreading_factor",
    "MESHCORE_CODING_RATE": "coding_rate",
    "MESHCORE_TX_POWER": "tx_power_dbm",
}


class SpiRadioError(DeviceCommandError):
    """The node could not be started, worded for the person who has to fix it.

    A :class:`~meshterm.core.connection.DeviceCommandError`, because that is what the
    startup picker and the connect path pass through as an *actionable* failure — shown
    with its remedy — rather than folding it into "didn't answer".

    Attributes:
        kind: What went wrong — ``unsupported``, ``runtime``, ``no-spi``, ``no-gpio``,
            ``permission``, ``busy``, or ``failed`` (see :func:`explain`).
    """

    def __init__(self, kind: str, message: str) -> None:
        """Carry ``message`` as the error text and ``kind`` as its classification."""
        super().__init__(message)
        self.kind = kind


# --- where things are ----------------------------------------------------------------------


def node_script() -> Path:
    """The node's source file, which is run by path under another interpreter.

    It sits beside this module in a checkout and in an install, and the one-file build
    ships it as a data file at the same relative place (``packaging/meshterm.spec``).
    """
    return Path(__file__).with_name("radionode.py")


def state_dir(config_dir: Path, wiring: SpiWiring) -> Path:
    """Where the node on ``wiring``'s SPI device keeps its identity, settings and lists."""
    return config_dir / "radio" / Path(wiring.spidev).name


def _on_linux() -> bool:
    """Whether this is Linux, the one place a radio on the SPI bus can be driven from."""
    return sys.platform.startswith("linux")


def _xdg_data_home() -> Path:
    """``$XDG_DATA_HOME``, or ``~/.local/share`` where it is unset."""
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".local" / "share"


def spi_present(wiring: SpiWiring) -> bool:
    """Whether ``wiring``'s SPI device node exists on this machine (Linux only)."""
    return _on_linux() and os.path.exists(wiring.spidev)


# --- finding a Python with the radio library -----------------------------------------------

#: Each interpreter probed this session: path -> whether it can import the radio library.
_probed: dict[str, bool] = {}


def _runtime_candidates(wiring: SpiWiring) -> list[str]:
    """Interpreters worth probing for the radio library, most specific first."""
    candidates: list[str] = []
    if wiring.python:
        candidates.append(os.path.expanduser(wiring.python))
    if not getattr(sys, "frozen", False):
        candidates.append(sys.executable)  # MeshTerm's own, installed with the ``spi`` extra
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

    A ``python`` named in the wiring is the only candidate when it is set — naming one is a
    decision, and quietly falling back past it would hide that it doesn't work.
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
    """The environment for a program MeshTerm starts, minus what a frozen build injected.

    The one-file build's bootloader leaves its own variables behind (``_PYI_*``,
    ``_MEIPASS2``) and points ``LD_LIBRARY_PATH`` at its unpacked libraries; another Python
    started with those loads MeshTerm's bundled copies of libssl and friends instead of its
    own. PyInstaller keeps the original under ``LD_LIBRARY_PATH_ORIG``, which is put back.
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


# --- who else has the radio ----------------------------------------------------------------


def radio_holder() -> str | None:
    """Name the program already holding the radio, when it is one we know how to stop.

    Asked before starting the node, because the node's own answer to a held pin is a bare
    "busy" after several seconds of retries, and the two usual holders each have a one-line
    fix worth saying instead.
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
    """The sentence a failed start is reported with: what is wrong and what to do about it."""
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
            f"(`sudo lsof /dev/gpiochip{wiring.gpio_chip}` shows which)"
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
        return f"/dev/gpiochip{wiring.gpio_chip} does not exist — check `gpio_chip` in the profile"
    if kind == "permission":
        return (
            f"{detail} — add yourself to the spi and gpio groups "
            "(`sudo usermod -aG spi,gpio $USER`) and log in again"
        )
    if kind == "unsupported":
        return detail
    return f"the radio node failed: {detail}"


# --- first use: carrying over the bridge's node --------------------------------------------


def _bridge_radio_seed() -> dict[str, int]:
    """The radio settings the bridge service runs with, from its unit's environment.

    The bridge takes its frequency, preset and power from ``MESHCORE_*`` variables, usually
    set in a drop-in; a node that inherits the bridge's identity should come up on the same
    channel of air, not on the default preset. Empty when there is no such service.
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
    """Copy the bridge's node into a radio's state directory the first time it is used.

    Runs only while the state directory has no identity yet, and copies rather than moves, so
    the bridge still works for anyone who goes back to it. The identity is looked for where
    the bridge itself looks, in its order: the ``meshcore-uconsole`` GUI's key first (the
    bridge shares it), then the bridge's own. Contacts come from the bridge's snapshot, whose
    records are the radio library's own contact dicts, so they load unchanged. Radio settings
    are seeded from the service's environment, and the node keeps the bridge's name.

    Args:
        state: The radio's state directory.

    Returns:
        What was carried over, for the log — empty when nothing was.
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
    """One running node: the child process and the loopback port it serves on.

    Attributes:
        process: The child.
        port: The loopback TCP port its frame server listens on.
        public_key: The node's public key, hex, as it reported it.
        log_path: Where its stderr goes.
    """

    process: asyncio.subprocess.Process
    port: int
    public_key: str
    log_path: Path

    @property
    def alive(self) -> bool:
        """Whether the child is still running."""
        return self.process.returncode is None

    async def stop(self) -> None:
        """End the node and wait for it: close its stdin, then SIGTERM, then SIGKILL.

        Closing stdin is the polite request — the node saves its contacts and frees the radio
        on the way out. The signals are for a node that has stopped listening, and each wait
        is bounded so a wedged child can never hold up a quit.
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
    """Keep the previous run's node log beside this one's, and no more."""
    if log_path.exists():
        try:
            log_path.replace(log_path.with_suffix(".log.1"))
        except OSError:
            pass


async def start_node(wiring: SpiWiring, state: Path, *, node_name: str) -> NodeProcess:
    """Start the node on ``wiring``'s radio and wait until it is listening.

    Args:
        wiring: How the radio is wired.
        state: The radio's state directory.
        node_name: The name a brand-new node advertises until it is renamed.

    Returns:
        The running node.

    Raises:
        SpiRadioError: If it can't be started, with the reason already worded.
    """
    if not _on_linux():
        raise SpiRadioError("unsupported", "a radio on the SPI bus needs Linux")
    # Each of these runs a short subprocess or two; none of them belongs on the event loop
    # that is drawing the "connecting" card.
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
            "use_dio2_rf": wiring.use_dio2_rf,
            "use_dio3_tcxo": wiring.use_dio3_tcxo,
            "is_waveshare": wiring.is_waveshare,
        },
        "seed": {"node_name": node_name, **DEFAULT_SEED},
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
            start_new_session=True,  # ^C in the terminal is MeshTerm's to handle, not the node's
        )
    node = NodeProcess(process=proc, port=0, public_key="", log_path=log_path)
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=READY_TIMEOUT_S)
    except asyncio.CancelledError:
        proc.kill()  # abandoned mid-start (a probe's own deadline): nothing may keep the radio
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
    """A companion connection to a node MeshTerm starts on the host's own radio.

    Underneath it is exactly a network companion on ``127.0.0.1`` — every command, event
    and trace goes through :class:`MeshCoreDevice`'s TCP path unchanged — with the node's
    lifetime tied to the connection's: :meth:`connect` starts it, :meth:`disconnect` ends
    it, and a reconnect (which disconnects and builds a fresh device) starts a fresh one.
    """

    def __init__(self, wiring: SpiWiring, state: Path, *, node_name: str = "MeshTerm") -> None:
        """Prepare the device; nothing is started until :meth:`connect`.

        Args:
            wiring: How the radio is wired.
            state: The radio's state directory (see :func:`state_dir`).
            node_name: The name a brand-new node advertises until it is renamed.
        """
        super().__init__(transport="tcp", host="127.0.0.1", tcp_port=0)
        self._wiring = wiring
        self._state = state
        self._node_name = node_name
        self._node: NodeProcess | None = None

    @property
    def transport(self) -> str:
        """``"spi"`` — the loopback socket underneath is an implementation detail."""
        return "spi"

    @property
    def endpoint(self) -> str:
        """The SPI device the radio is on, which is how the radio is named and remembered."""
        return self._wiring.spidev

    @property
    def wiring(self) -> SpiWiring:
        """How the radio this device drives is wired."""
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
            return False  # the node died, so the radio is gone whatever the socket says
        return await super().link_present()

    async def disconnect(self) -> None:  # noqa: D102 - inherited docstring
        try:
            await super().disconnect()
        finally:
            await self._stop_node()

    async def _stop_node(self) -> None:
        """End the node, if one is running, and forget it."""
        node, self._node = self._node, None
        if node is not None:
            await node.stop()

    def _no_response_message(self) -> str:
        """The node started but never answered as a companion: point at its log."""
        where = self._node.log_path if self._node is not None else self._state / "node.log"
        return f"the radio node on {self._wiring.spidev} did not answer; its log is {where}"
