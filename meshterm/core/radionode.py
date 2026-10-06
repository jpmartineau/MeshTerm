# SPDX-License-Identifier: Apache-2.0
"""MeshTerm runs this software MeshCore node on a radio that is wired directly to the host.

A board such as the AIO of the uConsole puts an SX1262 on the SPI bus of the host, with no
microcontroller and no firmware in front of it. Thus there is no companion to talk to
until a program runs one. This file is that program. MeshTerm starts it as a **child
process** when it connects (:mod:`meshterm.core.spiradio` is the half of the parent).
MeshTerm talks to it through the standard companion protocol on a loopback port, the same
as with a network companion. MeshTerm stops it when it disconnects. Thus the node holds
the pins of the radio for exactly as long as MeshTerm uses them.

**Self-contained on purpose.** No part of this file imports MeshTerm. The radio library
(``openhop_core``) is an optional install, and it is often in a different interpreter
from the MeshTerm interpreter. The one-file build cannot import it, and on a uConsole it
is usually in its own venv. Thus the parent runs this file by its path, with the Python
that has the library. The contract that makes this work is: the standard library and
``openhop_core``, and nothing else. ``tests/test_radionode.py`` makes sure that the
contract stays true.

**This file keeps what firmware keeps.** The library keeps prefs, channels, and contacts
only in memory. For this reason, a node that ran in this way once started with no
channels after each restart. Now all the data that a firmware companion keeps in flash is
kept in the state directory that the parent names:

=================  =============================================================
``identity.key``   the private seed of the node: its identity on the mesh
``prefs.json``     name, radio settings, TX power, position, the other prefs,
                   and also, on a board with a GPS, the GPS switch and interval
``channels.json``  the channel table, slot by slot
``contacts.json``  the contact list
=================  =============================================================

**This file does with a GPS what firmware does.** A board can have a wiring that names a
GPS port (the Cap of the Cardputer Zero). This board gets the two settings that the
MeshCore companion firmware gives to a board with a receiver: ``gps`` to run it, and
``gps_interval`` to set its rate. The node reports and sets them as custom variables,
because Device config in MeshTerm already looks for them there. While the GPS runs, a
valid fix becomes the position of the node. This is the position that its self-info
reports, and that its adverts carry when the node shares its location (:class:`Gps`).

**Messages to the parent.** The parent reads exactly one JSON line from the stdout of this
process: ``{"event": "ready", "port": …, "public_key": …}`` when the frame server
listens, or ``{"event": "error", "kind": …, "message": …}`` if the radio did not open.
The library prints its own diagnostics to stdout. Thus the node keeps the real stdout for
that one line only, and sends all other output to stderr. The parent keeps stderr as the
log of the node.

**The release of the radio.** The kernel releases the GPIO lines that the node requested
through the character device, and the open ``spidev``. It releases them when the process
ends, in any way that it ends. Thus the work is to make sure that the process ends with
its parent. The process exits when its stdin gets to end-of-file (when the parent closes
it, or when the parent dies and the kernel closes it). On Linux, it also asks for
``SIGTERM`` at the moment that its parent stops.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import functools
import json
import logging
import operator
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

#: The radio library that this node runs on. This file does not support ``pymc_core``
#: (the name of the library before 2026). It needs three shims that ``openhop_core`` made
#: unnecessary, and the standalone bridge still has them for the users of the old library.
RUNTIME = "openhop_core"

#: The number of times that the node tries ``radio.begin()`` when a GPIO line is busy, and
#: the pause before each retry. A node that exited released its lines when it stopped.
#: Thus the retries are for a line that a different program is in the process of
#: releasing. They are not to wait until a program that holds the line stops.
BEGIN_ATTEMPTS = 3
BEGIN_BACKOFF_S = 1.5

#: The model string that the frame server reports for a radio on a board that MeshTerm
#: does not know by name. MeshTerm shows it as the device model (the Cap reports
#: "Cap LoRa-1262").
DEVICE_MODEL = "MeshTerm SPI node"

log = logging.getLogger("radionode")


# --- errors that go to the parent ----------------------------------------------------------


class NodeError(Exception):
    """A failure to start the node, with the ``kind`` that the parent uses for its message.

    The kinds are: ``runtime`` (the radio library is missing or too old), ``no-spi`` /
    ``no-gpio`` / ``no-i2c`` (the device file does not exist), ``permission`` (it exists,
    but this user cannot open it), ``busy`` (a different program holds the pins of the
    radio), ``absent`` (the board is there, but the radio on it does not answer), and
    ``failed`` (all other failures).
    """

    def __init__(self, kind: str, message: str) -> None:
        """Keep ``message`` as the error text, and ``kind`` as its classification."""
        super().__init__(message)
        self.kind = kind


class _Recent(logging.Handler):
    """Keeps the recent error lines of the library, to tell why ``radio.begin()`` stopped.

    When a pin is busy or forbidden, the GPIO manager logs the reason and calls
    ``sys.exit``. Thus the reason exists only as a log line. The node reads it back from
    this handler.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())
        del self.lines[:-20]


def classify_begin_failure(lines: list[str]) -> str:
    """The error kind of a failed ``radio.begin()``, from the log lines of the library."""
    text = " ".join(lines).lower()
    if "already in use" in text or "resource busy" in text:
        return "busy"
    if "permission denied" in text:
        return "permission"
    return "failed"


# --- the state directory -------------------------------------------------------------------


def _write_json(path: Path, value: Any) -> None:
    """Replace ``path`` with ``value`` as JSON in one rename (after a crash, the old file stays)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    """``path`` parsed as JSON, or ``None`` when it is missing or cannot be read."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_identity_seed(state: Path, mint) -> bytes:  # noqa: ANN001 - () -> bytes
    """The private seed of the node from ``identity.key``, or a new one that it mints and saves.

    Args:
        state: The state directory of the node.
        mint: Makes a new seed (the key generator of the library) when no seed is saved
            yet.
    """
    path = state / "identity.key"
    try:
        seed = path.read_bytes()
    except OSError:
        seed = b""
    if seed:
        return seed
    seed = bytes(mint())
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(seed)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    log.info("minted a new node identity at %s", path)
    return seed


def prefs_to_json(prefs: Any) -> dict:
    """A ``NodePrefs`` as JSON-safe fields (``bytes`` as hex), for ``prefs.json``."""
    out = {}
    for field in dataclasses.fields(prefs):
        value = getattr(prefs, field.name)
        out[field.name] = value.hex() if isinstance(value, (bytes, bytearray)) else value
    return out


def apply_saved_prefs(prefs: Any, saved: Any) -> None:
    """Read ``prefs.json`` into a ``NodePrefs`` in place, one field at a time.

    The function takes only the fields that the ``NodePrefs`` of the library still
    declares. It converts each value to the type of the default of that field. Thus it
    ignores a field that the library removed. It also skips a value of the wrong type from
    a hand edit, and does not send it to the radio.
    """
    if not isinstance(saved, dict):
        return
    for field in dataclasses.fields(prefs):
        if field.name not in saved:
            continue
        current = getattr(prefs, field.name)
        raw = saved[field.name]
        try:
            if isinstance(current, (bytes, bytearray)):
                value: Any = bytes.fromhex(str(raw))
            elif isinstance(current, bool):
                value = bool(raw)
            elif isinstance(current, int):
                value = int(raw)
            elif isinstance(current, float):
                value = float(raw)
            else:
                value = str(raw)
        except (TypeError, ValueError):
            log.warning("prefs.json: ignoring %s=%r", field.name, raw)
            continue
        setattr(prefs, field.name, value)


def channels_to_json(store: Any) -> list[dict]:
    """The channel table as ``[{"idx", "name", "secret"}]`` in slot order."""
    out = []
    for idx in range(store.max_channels):
        channel = store.get(idx)
        if channel is not None and channel.name:
            out.append({"idx": idx, "name": channel.name, "secret": bytes(channel.secret).hex()})
    return out


def saved_channels(saved: Any) -> list[tuple[int, str, bytes]]:
    """``channels.json`` as ``(idx, name, secret)`` triples, without the malformed entries."""
    out = []
    for entry in saved if isinstance(saved, list) else []:
        try:
            out.append((int(entry["idx"]), str(entry["name"]), bytes.fromhex(entry["secret"])))
        except (KeyError, TypeError, ValueError):
            continue
    return [c for c in out if c[1]]


# --- the node ------------------------------------------------------------------------------


def preamble_for_sf(spreading_factor: int) -> int:
    """The LoRa preamble (symbols) that MeshCore uses at a spreading factor: 32 up to SF8, else 16.

    This is the rule of MeshCore itself (``RadioLibWrapper::preambleLengthForSF``). The
    receiver must also obey it, not only the transmitter. The SX1262 waits for the sync
    word only for approximately the length of the preamble that it expects. Thus a node
    that listens for 12 symbols, on a mesh that sends 32, locks on, stops, and locks on
    again later in the same preamble. It decodes a packet only when it locks on near the
    end by chance. That was the full cause of "the uConsole misses replies other radios
    hear": the preamble was a fixed 12 (the default of the library), and at SF7 the mesh
    sends 32.
    """
    return 32 if spreading_factor <= 8 else 16


def radio_kwargs(signature_params, wiring: dict, prefs: Any) -> dict:  # noqa: ANN001
    """The ``SX1262Radio`` constructor arguments: the wiring of the board and the saved radio.

    The function gives only the arguments that the constructor of this library version
    accepts. Thus, if the constructor does not have a parameter, the function removes it,
    and the constructor does not refuse the call. ``None`` means "not set", and the
    function never gives it.
    """
    wanted = dict(wiring)
    wanted.update(
        preamble_length=preamble_for_sf(prefs.spreading_factor),
        frequency=prefs.frequency_hz,
        bandwidth=prefs.bandwidth_hz,
        spreading_factor=prefs.spreading_factor,
        coding_rate=prefs.coding_rate,
        tx_power=prefs.tx_power_dbm,
    )
    return {k: v for k, v in wanted.items() if k in signature_params and v is not None}


def _check_device(path: str, missing_kind: str, what: str) -> None:
    """Refuse early, with the correct kind, when a device file is absent or cannot be opened."""
    if not os.path.exists(path):
        raise NodeError(missing_kind, f"{path} does not exist — is the {what} enabled?")
    if not os.access(path, os.R_OK | os.W_OK):
        raise NodeError("permission", f"no read/write permission on {path}")


# --- the board around the chip -------------------------------------------------------------

#: The directory where the kernel puts the switches that a device tree gives to
#: ``gpio-leds``.
LEDS = Path("/sys/class/leds")

#: The time that a header gets after it is powered on, before the node talks to a part on
#: it. The regulator and the expander of the Cap are ready well before this time. Also,
#: the chip does its own reset after this.
POWER_SETTLE_S = 0.1

#: ``I2C_SLAVE``: point the open ``/dev/i2c-*`` at one device. The kernel refuses it
#: (``EBUSY``) while a kernel driver is bound to that address. This is the correct answer,
#: because then the device is not ours to use.
_I2C_SLAVE = 0x0703

#: The registers of the PI4IOE5V6408, and the manufacturer field of its ID register (bits
#: 7–5). This field tells a different chip at the same address apart from it.
_PI4IO_ID, _PI4IO_DIRECTION, _PI4IO_OUTPUT, _PI4IO_HIGH_Z = 0x01, 0x03, 0x05, 0x07
_PI4IO_MAKER = 0b101


def set_leds(entries: list[str], root: Path = LEDS) -> list[tuple[Path, str]]:
    """Set each ``name=brightness`` switch, and return the old value of each, to put it back.

    A missing switch means that the wiring was written for a different board. If this user
    cannot write to a switch, the user is not in the ``gpio`` group, which is also
    necessary for the pins of the radio.
    """
    previous: list[tuple[Path, str]] = []
    for entry in entries:
        name, _, value = entry.partition("=")
        path = root / name / "brightness"
        if not path.exists():
            raise NodeError("failed", f"{path} does not exist — this board has no {name} switch")
        try:
            before = path.read_text(encoding="ascii").strip()
            path.write_text(value, encoding="ascii")
        except PermissionError:
            raise NodeError("permission", f"no write permission on {path}") from None
        previous.append((path, before))
    return previous


def restore_leds(previous: list[tuple[Path, str]]) -> None:
    """Put each switch back as :func:`set_leds` found it, the last one first (best effort)."""
    for path, before in reversed(previous):
        try:
            path.write_text(before, encoding="ascii")
        except OSError as exc:
            log.warning("could not put %s back to %s: %s", path, before, exc)


def drive_pi4io(bus: int, address: int, high: list[int], dev: Path = Path("/dev")) -> None:
    """Drive the ``high`` pins of a PI4IOE5V6408 high as outputs, and keep the rest as inputs.

    The function writes the output level before the direction. Thus a pin becomes an
    output that is already high, and does not glitch low first. The function reads the ID
    register first. On a board where this is the expander of the radio itself, no answer
    there means that the radio is not attached. That is the one failure that the message
    must give in those words.
    """
    path = dev / f"i2c-{bus}"
    _check_device(str(path), "no-i2c", "I2C bus")
    import fcntl

    mask = sum(1 << pin for pin in high) & 0xFF
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.ioctl(fd, _I2C_SLAVE, address)
        except OSError as exc:
            raise NodeError("busy", f"{address:#04x} on {path} is held: {exc}") from None
        try:
            os.write(fd, bytes([_PI4IO_ID]))
            ident = os.read(fd, 1)[0]
        except OSError:
            raise NodeError("absent", f"nothing answers at {address:#04x} on {path}") from None
        if ident >> 5 != _PI4IO_MAKER:
            raise NodeError(
                "absent", f"{address:#04x} on {path} is not a PI4IOE5V6408 (ID {ident:#04x})"
            )
        for register, value in (
            (_PI4IO_OUTPUT, mask),
            (_PI4IO_DIRECTION, mask),
            (_PI4IO_HIGH_Z, 0xFF & ~mask),
        ):
            os.write(fd, bytes([register, value]))
    finally:
        os.close(fd)
    log.info("PI4IO %#04x on %s: pins %s high", address, path, high)


def prepare_board(wiring: dict) -> list[tuple[Path, str]]:
    """Switch on the parts that the chip must have before the radio library uses it.

    First the LED-class switches of the board (power, pin routing), then its RF expander.
    The node can get to the expander only after the header that it is on has power. The
    function returns the previous states of the switches. The caller puts them back when
    the node ends, also when the node did not start. Thus a failed start leaves the header
    as it was.
    """
    previous = set_leds(list(wiring.get("leds") or ()), root=LEDS)
    if previous:
        time.sleep(POWER_SETTLE_S)
    try:
        if int(wiring.get("pi4io_bus", -1)) >= 0:
            drive_pi4io(
                int(wiring["pi4io_bus"]),
                int(wiring.get("pi4io_address", 0x43)),
                list(wiring.get("pi4io_high") or ()),
            )
    except BaseException:
        restore_leds(previous)
        raise
    return previous


# --- the GPS beside the chip ---------------------------------------------------------------

#: The custom variables of a board with a GPS, with the names of the MeshCore firmware.
GPS_VARS = ("gps", "gps_interval")

#: The maximum of ``gps_interval`` in the firmware (``constrain(…, 0, 86400)``): one day.
GPS_INTERVAL_MAX_S = 86400

#: NMEA lets a sentence have 82 characters. A partial line that is much longer is noise on
#: the wire, not a sentence that is still arriving. Thus the node removes it, and does not
#: make it longer.
_NMEA_MAX = 512


def nmea_fix(line: str) -> tuple[float, float] | None:
    """The position that one NMEA sentence reports as a valid fix, as ``(lat, lon)``, or ``None``.

    Two sentences carry a fix, from the talker of any constellation (``GP``, ``GN``,
    ``GL``, ``GA``, ``GB``…): RMC, which is valid when its status is ``A``, and GGA, which
    is valid when its fix quality is not ``0``. All other input gives ``None``: a different
    sentence, a sentence with a checksum that does not match (a byte lost on the wire), a
    field that is not a coordinate, and the no-fix sentences that a receiver sends until it
    has a fix.
    """
    line = line.strip()
    body, star, given = line.removeprefix("$").partition("*")
    if not line.startswith("$") or not star or len(given) < 2:
        return None
    try:
        if int(given[:2], 16) != functools.reduce(operator.xor, body.encode("ascii", "replace"), 0):
            return None
    except ValueError:
        return None
    fields = body.split(",")
    kind = fields[0][-3:]
    if kind == "RMC" and len(fields) > 6 and fields[2] == "A":
        coords = fields[3:7]
    elif kind == "GGA" and len(fields) > 6 and fields[6] not in ("", "0"):
        coords = fields[2:6]
    else:
        return None
    try:
        return _degrees(coords[0], coords[1], "NS", 2), _degrees(coords[2], coords[3], "EW", 3)
    except ValueError:
        return None


def _degrees(value: str, hemisphere: str, hemispheres: str, degree_digits: int) -> float:
    """The NMEA ``ddmm.mmmm`` (``dddmm.mmmm`` east–west) and its hemisphere, as signed degrees."""
    if len(hemisphere) != 1 or hemisphere not in hemispheres or len(value) <= degree_digits:
        raise ValueError(value)
    minutes = float(value[degree_digits:])
    unsigned = int(value[:degree_digits]) + minutes / 60
    if not 0 <= minutes < 60 or unsigned > (90 if degree_digits == 2 else 180):
        raise ValueError(value)
    return -unsigned if hemisphere == hemispheres[1] else unsigned


def _saved_int(saved: dict, key: str, default: int) -> int:
    """``saved[key]`` as an int, or ``default`` if it is missing or is not an int."""
    try:
        return int(saved.get(key, default))
    except (TypeError, ValueError):
        return default


class Gps:
    """The GPS receiver of the board, which runs in the same way as in MeshCore companion firmware.

    There are two settings, with the names and the defaults of the firmware. ``gps`` is off
    until the user switches it on, and the node remembers it across restarts.
    ``gps_interval`` is the number of seconds between position updates. ``0`` (the
    default) takes each fix, which is one fix each second in the firmware. The node reads
    the NMEA of the receiver on its own event loop (some sentences each second is a very
    small load compared to the radio). It writes a valid fix into the prefs of the node.
    The self-info frame and the adverts of the node read the position from there. Firmware
    keeps a fix only in RAM. Here, the node saves the position with its other prefs each
    time that it saves them, and one more time when the node shuts down. Thus, after a
    restart indoors, the node starts from the last fix, not from a position that the user
    set by hand long ago.

    Attributes:
        port: The serial port of the receiver.
        baud: Its line speed.
        enabled: Whether ``gps`` is on.
        interval_s: ``gps_interval``.
        node: The companion whose position a fix moves. It is set when the companion
            exists.
        moved: Whether a fix has moved the position since the last save of the prefs.
    """

    def __init__(self, port: str, baud: int, saved: Any = None) -> None:
        """Take the port and speed from the wiring, and the two settings from ``prefs.json``."""
        saved = saved if isinstance(saved, dict) else {}
        self.port = port
        self.baud = baud
        self.enabled = _saved_int(saved, "gps_enabled", 0) == 1
        self.interval_s = min(max(_saved_int(saved, "gps_interval", 0), 0), GPS_INTERVAL_MAX_S)
        self.node: Any = None
        self.moved = False
        self._fd: int | None = None
        self._pending = b""
        self._next_at = 0.0
        self._fixed = False

    @property
    def running(self) -> bool:
        """Whether the port of the receiver is open, and the node reads it."""
        return self._fd is not None

    def custom_vars(self) -> dict[str, str]:
        """The two settings as the firmware reports them: ``gps`` tells whether the GPS runs."""
        return {"gps": "1" if self.running else "0", "gps_interval": str(self.interval_s)}

    def saved(self) -> dict[str, int]:
        """The two settings as ``prefs.json`` keeps them, with the field names of the firmware."""
        return {"gps_enabled": int(self.enabled), "gps_interval": self.interval_s}

    def set_var(self, name: str, value: str) -> bool:
        """Set ``gps`` or ``gps_interval``. ``False`` refuses the value and changes nothing."""
        value = value.strip()
        if name == "gps" and value in ("0", "1"):
            if value == "1":
                try:
                    self.start()
                except OSError as exc:
                    log.warning("GPS on %s did not open: %s", self.port, exc)
                    return False
            else:
                self.stop()
            self.enabled = value == "1"
            return True
        if name == "gps_interval":
            try:
                seconds = int(value)
            except ValueError:
                return False
            self.interval_s = min(max(seconds, 0), GPS_INTERVAL_MAX_S)
            self._next_at = 0.0  # the new rate starts at the next fix
            return True
        return False

    def start(self) -> None:
        """Open the port raw, at the speed of the receiver, and read it on the running loop.

        Raises:
            OSError: The port cannot be opened or set up (it is missing, not ours, or not a
                tty).
        """
        if self._fd is not None:
            return
        import termios
        import tty

        speed = getattr(termios, f"B{self.baud}", None)
        if speed is None:
            raise OSError(f"{self.baud} is not a line speed this system has")
        fd = os.open(self.port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            attrs[4] = attrs[5] = speed  # input and output speed
            attrs[2] |= termios.CLOCAL | termios.CREAD
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
            asyncio.get_running_loop().add_reader(fd, self._readable)
        except termios.error as exc:
            os.close(fd)
            raise OSError(f"{self.port} is not a serial port ({exc})") from None
        except BaseException:
            os.close(fd)
            raise
        self._fd, self._pending, self._next_at, self._fixed = fd, b"", 0.0, False
        log.info("GPS on %s at %d baud", self.port, self.baud)

    def stop(self) -> None:
        """Stop the read and close the port. The position keeps the last fix."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(fd)
        except RuntimeError:  # no loop to remove it from: the close is sufficient
            pass
        os.close(fd)
        log.info("GPS on %s stopped", self.port)

    def _readable(self) -> None:
        """Read the data that the port has. Stop a port that hangs up or fails, with no retry."""
        try:
            chunk = os.read(self._fd, 4096)  # type: ignore[arg-type]
        except BlockingIOError:
            return
        except OSError as exc:
            log.warning("GPS on %s failed: %s", self.port, exc)
            self.stop()
            return
        if not chunk:  # hung up: if it is not stopped, it reads as ready forever
            log.warning("GPS on %s hung up", self.port)
            self.stop()
            return
        self.feed(chunk)

    def feed(self, chunk: bytes, now: float | None = None) -> None:
        """Take bytes when they arrive, and act on each complete sentence in them."""
        *lines, self._pending = (self._pending + chunk).split(b"\n")
        if len(self._pending) > _NMEA_MAX:
            self._pending = b""
        for raw in lines:
            fix = nmea_fix(raw.decode("ascii", "replace"))
            if fix is not None:
                self._take(fix, time.monotonic() if now is None else now)

    def _take(self, fix: tuple[float, float], now: float) -> None:
        """Make ``fix`` the position of the node, one time in each interval."""
        if self.node is None or now < self._next_at:
            return
        if not self._fixed:  # one time per start, never the position: users paste logs in issues
            log.info("GPS has a fix")
            self._fixed = True
        prefs = self.node.prefs
        if (prefs.latitude, prefs.longitude) != fix:
            prefs.latitude, prefs.longitude = fix
            self.moved = True
        self._next_at = now + max(self.interval_s, 1)


def _persist_contacts(store: Any, path: Path) -> None:
    """Write a snapshot of the contact list after each change, as the firmware writes flash."""

    def save() -> None:
        try:
            _write_json(path, store.to_dicts())
        except Exception as exc:  # noqa: BLE001 - a failed snapshot must not stop the node
            log.warning("could not save contacts: %s", exc)

    for name in ("add", "add_or_overwrite", "update", "remove", "clear"):
        original = getattr(store, name, None)
        if original is None:
            continue

        def wrapped(*args, _original=original, **kwargs):  # noqa: ANN002, ANN003, ANN202
            result = _original(*args, **kwargs)
            save()
            return result

        setattr(store, name, wrapped)
    store.save_snapshot = save


async def run_node(config: dict, report) -> int:  # noqa: ANN001 - (dict) -> None
    """Start the node, report ready, and serve until the parent tells it to stop.

    Args:
        config: ``state_dir``, ``wiring`` (the pins and switches of the board), ``seed``
            (name and radio settings for a node that has no ``prefs.json`` yet), and
            ``model`` (the name of the board, reported as the device model. Without it,
            :data:`DEVICE_MODEL`).
        report: Sends the one status line to the parent.

    Returns:
        The exit status of the process.
    """
    import importlib

    try:
        rt = importlib.import_module(RUNTIME)
        companion_mod = importlib.import_module(f"{RUNTIME}.companion")
        models = importlib.import_module(f"{RUNTIME}.companion.models")
        identity_mod = importlib.import_module(f"{RUNTIME}.protocol.identity")
        sx1262 = importlib.import_module(f"{RUNTIME}.hardware.sx1262_wrapper")
    except ImportError as exc:
        raise NodeError("runtime", f"{RUNTIME} is not importable here ({exc})") from exc
    log.info("node runtime %s %s (%s)", RUNTIME, getattr(rt, "__version__", "?"), sys.executable)

    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    wiring = dict(config.get("wiring") or {})
    seed = dict(config.get("seed") or {})

    bus, cs = int(wiring.get("bus_id", 0)), int(wiring.get("cs_id", 0))
    _check_device(f"/dev/spidev{bus}.{cs}", "no-spi", "SPI overlay")
    _check_device(f"/dev/gpiochip{int(wiring.get('gpio_chip', 0))}", "no-gpio", "GPIO chip")

    # The switches of the board go back to how they were, in any way that the node ends,
    # also when the radio did not start. Thus a failed start leaves the header as it was.
    switched = prepare_board(wiring)
    try:
        return await _serve(
            state,
            wiring,
            seed,
            report,
            model=str(config.get("model") or DEVICE_MODEL),
            models=models,
            sx1262=sx1262,
            companion_mod=companion_mod,
            identity_mod=identity_mod,
        )
    finally:
        restore_leds(switched)


async def _serve(  # noqa: PLR0913 - the library modules run_node imported, handed on
    state: Path,
    wiring: dict,
    seed: dict,
    report,  # noqa: ANN001 - (dict) -> None
    *,
    model: str,
    models: Any,
    sx1262: Any,
    companion_mod: Any,
    identity_mod: Any,
) -> int:
    """Start the radio on a prepared board, report ready, and serve until told to stop."""
    import inspect

    # The saved prefs decide the radio settings with which the chip starts. The seed only
    # gives values to a node that has never saved prefs.
    prefs = models.NodePrefs(
        node_name=seed.get("node_name", "MeshTerm"),
        tx_power_dbm=seed.get("tx_power_dbm", 22),
        frequency_hz=seed.get("frequency_hz", 910_525_000),
        bandwidth_hz=seed.get("bandwidth_hz", 62_500),
        spreading_factor=seed.get("spreading_factor", 7),
        coding_rate=seed.get("coding_rate", 5),
    )
    saved_prefs = _read_json(state / "prefs.json")
    apply_saved_prefs(prefs, saved_prefs)
    gps = (
        Gps(str(wiring["gps_port"]), int(wiring.get("gps_baud") or 9600), saved_prefs)
        if wiring.get("gps_port")
        else None
    )

    SX1262Radio = sx1262.SX1262Radio
    kwargs = radio_kwargs(inspect.signature(SX1262Radio.__init__).parameters, wiring, prefs)
    log.info("radio config %s", " ".join(f"{k}={v}" for k, v in kwargs.items()))
    radio = SX1262Radio(**kwargs)

    recent = _Recent()
    logging.getLogger().addHandler(recent)
    for attempt in range(BEGIN_ATTEMPTS):
        recent.lines.clear()
        try:
            if radio.begin():
                break
            kind = classify_begin_failure(recent.lines)
        except SystemExit:  # the answer of the GPIO manager to a busy or forbidden pin
            kind = classify_begin_failure(recent.lines)
        except Exception as exc:  # noqa: BLE001 - reported to the parent, not raised
            recent.lines.append(str(exc))
            kind = classify_begin_failure(recent.lines)
        try:
            radio.cleanup()
        except Exception:  # noqa: BLE001, S110 - best-effort release before retrying
            pass
        if kind != "busy" or attempt == BEGIN_ATTEMPTS - 1:
            detail = recent.lines[-1] if recent.lines else "radio.begin() failed"
            raise NodeError(kind, detail)
        await asyncio.sleep(BEGIN_BACKOFF_S * (attempt + 1))
    logging.getLogger().removeHandler(recent)
    log.info("radio ready")

    try:
        seed_bytes = load_identity_seed(
            state, lambda: identity_mod.LocalIdentity().get_signing_key_bytes()
        )
        identity = identity_mod.LocalIdentity(seed_bytes)
        node = _persistent_companion(companion_mod.CompanionRadio, state, gps)(
            radio,
            identity,
            node_name=prefs.node_name,
            radio_config={
                "frequency": prefs.frequency_hz,
                "bandwidth": prefs.bandwidth_hz,
                "spreading_factor": prefs.spreading_factor,
                "coding_rate": prefs.coding_rate,
                "tx_power": prefs.tx_power_dbm,
            },
        )
        apply_saved_prefs(node.prefs, prefs_to_json(prefs))

        for idx, name, secret in saved_channels(_read_json(state / "channels.json")):
            node.set_channel(idx, name, secret)
        contacts = _read_json(state / "contacts.json")
        if isinstance(contacts, list):
            node.contacts.load_from_dicts(contacts)
            log.info("restored %d contact(s)", node.contacts.get_count())
        _persist_contacts(node.contacts, state / "contacts.json")
        node.add_push_callback(
            "channel_updated",
            lambda _idx, _channel: _write_json(
                state / "channels.json", channels_to_json(node.channels)
            ),
        )

        await node.start()
        if gps is not None:
            gps.node = node
            if gps.enabled:
                try:
                    gps.start()
                except OSError as exc:  # the radio still works, and `gps` reads 0 until it opens
                    log.warning("GPS on %s did not open: %s", gps.port, exc)
        public_key = node.get_public_key()
        server = companion_mod.CompanionFrameServer(
            bridge=node,
            companion_hash=f"{public_key[0]:02x}",
            port=0,
            bind_address="127.0.0.1",
            local_hash=public_key[0],
            device_model=model,
            client_idle_timeout_sec=None,
        )
        await server.start()
        # The one push that the frame server of the library does not subscribe to: each
        # overheard packet with its SNR/RSSI. The live feed of MeshTerm is built from these.
        node.add_push_callback("rx_log_data", server.push_rx_raw)
        port = server._server.sockets[0].getsockname()[1]
    except Exception:
        radio.cleanup()
        raise

    log.info("node %s… listening on 127.0.0.1:%d", public_key.hex()[:16], port)
    report({"event": "ready", "port": port, "public_key": public_key.hex()})

    try:
        await _until_told_to_stop()
    finally:
        log.info("shutting down")
        if gps is not None:
            gps.stop()
            if gps.moved:
                node._save_prefs()  # the last fix, thus a restart starts where the node was
        for step in (server.stop, node.stop):
            try:
                await step()
            except Exception as exc:  # noqa: BLE001 - keep going: the radio still needs freeing
                log.warning("shutdown step failed: %s", exc)
        node.contacts.save_snapshot()
        _forget_edge_threads(radio)
        radio.cleanup()
    return 0


def _forget_edge_threads(radio: Any) -> None:
    """Save the radio cleanup two seconds of wait for a thread that cannot hear it.

    The library watches the IRQ pin from a daemon thread that waits in a 30-second
    ``poll``. The stop event of the thread cannot cut this poll short. ``cleanup()`` joins
    that thread for a maximum of two seconds before it closes the pins. Thus each shutdown
    waited the full two seconds for no result. On the Cardputer Zero, that was 2.2 s of a
    3.4 s exit, and the launcher gives only three seconds between its SIGTERM and its
    SIGKILL (measured 2026-10-06). The thread is a daemon, and the kernel releases its line
    when the process ends. Thus this function tells the thread to stop, then removes it
    from the thread list of the manager. Then the cleanup closes the pins and does not wait
    for the thread. If a library keeps its threads in a different place, this function
    does nothing: the cleanup is slower, but not wrong.
    """
    manager = getattr(radio, "_gpio_manager", None)
    stops = getattr(manager, "_edge_stop_events", None)
    threads = getattr(manager, "_edge_threads", None)
    if not isinstance(stops, dict) or not isinstance(threads, dict):
        return
    for stop in stops.values():
        stop.set()
    threads.clear()


def apply_preamble(radio: Any, symbols: int) -> None:
    """Give a running radio a new preamble length, for the packets that it transmits and hears.

    The driver reads ``preamble_length`` again for each transmission. But the receive path
    keeps the last packet parameters that it got. Thus the function sends these parameters
    again from standby, and then puts the chip back in listen mode.
    """
    if getattr(radio, "preamble_length", symbols) == symbols:
        return
    radio.preamble_length = symbols
    lora = getattr(radio, "lora", None)
    if lora is None:
        return
    try:
        lora.setStandby(lora.STANDBY_RC)
        lora.setPacketParamsLoRa(symbols, lora.HEADER_EXPLICIT, 64, lora.CRC_ON, lora.IQ_STANDARD)
        lora.request(lora.RX_CONTINUOUS)
        log.info("preamble set to %d symbols", symbols)
    except Exception as exc:  # noqa: BLE001 - the next transmission sets it regardless
        log.warning("could not apply the new preamble while listening: %s", exc)


def _persistent_companion(base: type, state: Path, gps: Gps | None = None) -> type:
    """``CompanionRadio``, which writes its prefs to ``prefs.json`` at each change.

    ``_save_prefs`` is the hook of the library for exactly this purpose ("subclasses that
    need persistence … should override this method"). The library calls it after each
    prefs setter. With a ``gps``, the two GPS settings of the board are in its custom
    variables, as the firmware lists them. The class saves them next to the prefs that the
    library knows.
    """

    class PersistentCompanion(base):  # type: ignore[misc, valid-type]
        def set_radio_params(self, freq_hz: int, bw_hz: int, sf: int, cr: int) -> bool:
            """Tune again, and also change the preamble when the spreading factor changes it.

            The library tunes the modulation again, but does not change the packet
            parameters, and the preamble is one of them (refer to :func:`preamble_for_sf`).
            """
            ok = super().set_radio_params(freq_hz, bw_hz, sf, cr)
            if ok:
                apply_preamble(self._radio, preamble_for_sf(sf))
            return ok

        def get_custom_vars(self) -> dict[str, str]:
            """The variables of the library, and the two of a GPS board as firmware reports them."""
            found = super().get_custom_vars()
            if gps is not None:
                found.update(gps.custom_vars())
            return found

        def set_custom_var(self, name: str, value: str) -> bool:
            """Set a variable. ``gps`` and ``gps_interval`` control the receiver and are saved."""
            if gps is None or name not in GPS_VARS:
                return super().set_custom_var(name, value)
            if not gps.set_var(name, value):
                return False
            self._save_prefs()
            return True

        def _save_prefs(self) -> None:
            try:
                saved = prefs_to_json(self.prefs)
                if gps is not None:
                    saved.update(gps.saved())
                _write_json(state / "prefs.json", saved)
                if gps is not None:
                    gps.moved = False
            except Exception as exc:  # noqa: BLE001 - a failed save must not fail the setter
                log.warning("could not save prefs: %s", exc)

    return PersistentCompanion


async def _until_told_to_stop() -> None:
    """Wait until stdin closes, or for a SIGTERM/SIGINT: the two ways that the parent says stop."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    def watch_stdin() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=watch_stdin, name="parent-watch", daemon=True).start()
    await stop.wait()


def _die_with_parent() -> None:
    """On Linux, make the kernel send SIGTERM when the parent exits (best effort)."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    """The entry point: ``python radionode.py --config '<json>'``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="the node's configuration, as JSON")
    args = parser.parse_args(argv)

    # One line on the real stdout is for the parent. All other output goes to stderr.
    status = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def report(message: dict) -> None:
        status.write(json.dumps(message) + "\n")
        status.flush()

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _die_with_parent()
    try:
        return asyncio.run(run_node(json.loads(args.config), report))
    except NodeError as exc:
        log.error("%s: %s", exc.kind, exc)
        report({"event": "error", "kind": exc.kind, "message": str(exc)})
        return 1
    except Exception as exc:  # noqa: BLE001 - the parent gets a sentence, the log the trace
        log.exception("node failed")
        report({"event": "error", "kind": "failed", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
