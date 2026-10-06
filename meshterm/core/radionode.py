# SPDX-License-Identifier: Apache-2.0
"""The software MeshCore node MeshTerm runs on a radio wired straight to the host.

A board like the uConsole's AIO puts an SX1262 on the host's SPI bus with no
microcontroller and no firmware in front of it, so there is no companion to talk to until
something runs one. This file is that something: MeshTerm starts it as a **child process**
when it connects (:mod:`meshterm.core.spiradio` is the parent's half), talks to it over the
standard companion protocol on a loopback port like any network companion, and ends it when
it disconnects — so the radio's pins are held exactly as long as MeshTerm is using them.

**Self-contained on purpose.** Nothing here imports MeshTerm. The radio library
(``openhop_core``) is an optional install that often lives in a different interpreter than
MeshTerm's — the one-file build cannot import it at all, and on a uConsole it usually sits
in its own venv — so the parent runs *this file* by path under whichever Python has the
library. Standard library plus ``openhop_core``, and nothing else, is the contract that
makes that work; ``tests/test_radionode.py`` holds it.

**What firmware would remember, this remembers.** The library keeps preferences, channels
and contacts in memory only, which is why a node run this way used to come up with no
channels after every restart. Everything a firmware companion keeps in flash is kept in the
state directory the parent names instead:

=================  =============================================================
``identity.key``   the node's private seed — its identity on the mesh
``prefs.json``     name, radio settings, TX power, position, the other prefs —
                   and, on a board with a GPS, its switch and interval
``channels.json``  the channel table, slot by slot
``contacts.json``  the contact list
=================  =============================================================

**What firmware would do with a GPS, this does.** A board whose wiring names a GPS port
(the Cardputer Zero's Cap) gets the two settings MeshCore's companion firmware gives a
board with a receiver — ``gps`` to run it and ``gps_interval`` to pace it — reported and
set as custom variables, which is where MeshTerm's Device config already looks for them.
While it runs, a valid fix becomes the node's position: the one its self-info reports
and, when the node shares its location, its adverts carry (:class:`Gps`).

**Talking to the parent.** The parent reads exactly one JSON line from this process's
stdout: ``{"event": "ready", "port": …, "public_key": …}`` once the frame server is
listening, or ``{"event": "error", "kind": …, "message": …}`` if the radio could not be
opened. The library prints its own diagnostics to stdout, so the real stdout is set aside
for that one line and everything else is sent to stderr, which the parent keeps as the
node's log.

**Letting go of the radio.** GPIO lines requested through the character device and an open
``spidev`` are the kernel's to release, and it releases them when the process ends however
it ends. The work is making sure the process *does* end with its parent: it exits when its
stdin reaches end-of-file (the parent closing it, or the parent dying and the kernel closing
it), and on Linux it also asks for ``SIGTERM`` the moment its parent goes.
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

#: The radio library this node runs on. ``pymc_core`` (its name before 2026) is not
#: supported here: it needs three shims that ``openhop_core`` made unnecessary, and the
#: standalone bridge still carries them for anyone on it.
RUNTIME = "openhop_core"

#: How many times ``radio.begin()`` is tried when a GPIO line is busy, and the pause before
#: each retry. A node that just exited released its lines as it went, so the retries are for
#: a line some *other* program is letting go of — not for waiting out a program that holds it.
BEGIN_ATTEMPTS = 3
BEGIN_BACKOFF_S = 1.5

#: The model string the frame server reports, which MeshTerm shows as the device model.
DEVICE_MODEL = "MeshTerm SPI node"

log = logging.getLogger("radionode")


# --- errors the parent is told about -------------------------------------------------------


class NodeError(Exception):
    """A failure to bring the node up, with the ``kind`` the parent words its message by.

    Kinds: ``runtime`` (the radio library is missing or too old), ``no-spi`` / ``no-gpio`` /
    ``no-i2c`` (the device node doesn't exist), ``permission`` (it exists but this user
    can't open it), ``busy`` (another program holds the radio's pins), ``absent`` (the board
    is there but the radio on it doesn't answer), and ``failed`` (anything else).
    """

    def __init__(self, kind: str, message: str) -> None:
        """Carry ``message`` as the error text and ``kind`` as its classification."""
        super().__init__(message)
        self.kind = kind


class _Recent(logging.Handler):
    """Keeps the library's recent error lines, to tell *why* ``radio.begin()`` gave up.

    The GPIO manager answers a busy or forbidden pin by logging the reason and calling
    ``sys.exit``, so the reason exists only as a log line; this is where it is read back.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())
        del self.lines[:-20]


def classify_begin_failure(lines: list[str]) -> str:
    """The error kind a failed ``radio.begin()`` amounts to, from the library's log lines."""
    text = " ".join(lines).lower()
    if "already in use" in text or "resource busy" in text:
        return "busy"
    if "permission denied" in text:
        return "permission"
    return "failed"


# --- the state directory -------------------------------------------------------------------


def _write_json(path: Path, value: Any) -> None:
    """Replace ``path`` with ``value`` as JSON in one rename (a crash keeps the old file)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    """``path`` parsed as JSON, or ``None`` when it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_identity_seed(state: Path, mint) -> bytes:  # noqa: ANN001 - () -> bytes
    """The node's private seed from ``identity.key``, minting and saving one if there is none.

    Args:
        state: The node's state directory.
        mint: Makes a fresh seed (the library's key generator) when none is saved yet.
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
    """Load ``prefs.json`` into a ``NodePrefs`` in place, field by field.

    Only fields the library's ``NodePrefs`` still declares are taken, each coerced to the
    type its default has, so a field the library dropped is ignored and a hand-edited value
    of the wrong type is skipped rather than carried into the radio.
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
    """``channels.json`` as ``(idx, name, secret)`` triples, skipping malformed entries."""
    out = []
    for entry in saved if isinstance(saved, list) else []:
        try:
            out.append((int(entry["idx"]), str(entry["name"]), bytes.fromhex(entry["secret"])))
        except (KeyError, TypeError, ValueError):
            continue
    return [c for c in out if c[1]]


# --- the node ------------------------------------------------------------------------------


def preamble_for_sf(spreading_factor: int) -> int:
    """The LoRa preamble, in symbols, MeshCore uses at a spreading factor: 32 up to SF8, else 16.

    This is MeshCore's own rule (``RadioLibWrapper::preambleLengthForSF``), and the receiver
    has to follow it, not only the transmitter. The SX1262 waits for the sync word only about
    as long as the preamble it was told to expect, so a node listening for 12 symbols against
    a mesh sending 32 locks on, gives up, and locks on again further along the same preamble
    — and decodes a packet only when it happens to lock on near the end. That was the whole
    of "the uConsole misses replies other radios hear": the preamble was a fixed 12, the
    library's default, and at SF7 the mesh sends 32.
    """
    return 32 if spreading_factor <= 8 else 16


def radio_kwargs(signature_params, wiring: dict, prefs: Any) -> dict:  # noqa: ANN001
    """The ``SX1262Radio`` constructor arguments: the board's wiring plus the saved radio.

    Passes only what this library version's constructor accepts, so a knob it lacks is
    dropped instead of refusing the call. ``None`` means "not set" and is never passed.
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
    """Refuse early, with the right kind, when a device node is absent or unopenable."""
    if not os.path.exists(path):
        raise NodeError(missing_kind, f"{path} does not exist — is the {what} enabled?")
    if not os.access(path, os.R_OK | os.W_OK):
        raise NodeError("permission", f"no read/write permission on {path}")


# --- the board around the chip -------------------------------------------------------------

#: Where the kernel puts the switches a device tree hands to ``gpio-leds``.
LEDS = Path("/sys/class/leds")

#: How long a header that was just powered gets before anything on it is spoken to. The
#: Cap's regulator and expander are up well inside this; the chip's own reset follows anyway.
POWER_SETTLE_S = 0.1

#: ``I2C_SLAVE``: address the open ``/dev/i2c-*`` at one device. Refused (``EBUSY``) while a
#: kernel driver is bound to that address, which is the right answer — it is then not ours.
_I2C_SLAVE = 0x0703

#: The PI4IOE5V6408's registers, and the manufacturer field of its ID register (bits 7–5),
#: which is how a different chip at the same address is told apart.
_PI4IO_ID, _PI4IO_DIRECTION, _PI4IO_OUTPUT, _PI4IO_HIGH_Z = 0x01, 0x03, 0x05, 0x07
_PI4IO_MAKER = 0b101


def set_leds(entries: list[str], root: Path = LEDS) -> list[tuple[Path, str]]:
    """Set each ``name=brightness`` switch, and return what each read before, to put back.

    A switch that is missing means a wiring written for another board; one this user can't
    write is the ``gpio`` group the radio's pins need anyway.
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
    """Put each switch back as :func:`set_leds` found it, last first (best-effort)."""
    for path, before in reversed(previous):
        try:
            path.write_text(before, encoding="ascii")
        except OSError as exc:
            log.warning("could not put %s back to %s: %s", path, before, exc)


def drive_pi4io(bus: int, address: int, high: list[int], dev: Path = Path("/dev")) -> None:
    """Drive ``high``'s pins of a PI4IOE5V6408 high, as outputs, and leave the rest inputs.

    The output level is written before the direction, so a pin becomes an output already
    high rather than glitching low first. The ID register is read first: on a board where
    this is the radio's own expander, silence there is the radio not being attached — the
    one failure worth saying in those words.
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
    """Switch on what the chip needs before the radio library touches it.

    The board's LED-class switches first (power, pin routing), then its RF expander, which
    is only reachable once the header it sits on is powered. Returns the switches' previous
    states, which the caller puts back when the node ends — including when it never got
    going, so a failed start leaves the header as it found it.
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

#: The custom variables a board with a GPS answers to, by MeshCore firmware's own names.
GPS_VARS = ("gps", "gps_interval")

#: The firmware's ceiling on ``gps_interval`` (``constrain(…, 0, 86400)``): one day.
GPS_INTERVAL_MAX_S = 86400

#: NMEA allows a sentence 82 characters; a partial line well past that is noise on the wire,
#: not a sentence still arriving, and is dropped rather than grown.
_NMEA_MAX = 512


def nmea_fix(line: str) -> tuple[float, float] | None:
    """The position one NMEA sentence reports as a valid fix, as ``(lat, lon)``, or ``None``.

    Two sentences carry a fix, from any constellation's talker (``GP``, ``GN``, ``GL``,
    ``GA``, ``GB``…): RMC, valid when its status is ``A``, and GGA, valid when its fix
    quality isn't ``0``. Everything else is ``None`` — another sentence, a sentence whose
    checksum doesn't match (a byte lost on the wire), a field that isn't a coordinate, and
    the no-fix sentences a receiver sends until it has one.
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
    """NMEA's ``ddmm.mmmm`` (``dddmm.mmmm`` east–west) and its hemisphere, as signed degrees."""
    if len(hemisphere) != 1 or hemisphere not in hemispheres or len(value) <= degree_digits:
        raise ValueError(value)
    minutes = float(value[degree_digits:])
    unsigned = int(value[:degree_digits]) + minutes / 60
    if not 0 <= minutes < 60 or unsigned > (90 if degree_digits == 2 else 180):
        raise ValueError(value)
    return -unsigned if hemisphere == hemispheres[1] else unsigned


def _saved_int(saved: dict, key: str, default: int) -> int:
    """``saved[key]`` as an int, or ``default`` where it is missing or isn't one."""
    try:
        return int(saved.get(key, default))
    except (TypeError, ValueError):
        return default


class Gps:
    """The board's GPS receiver, run the way MeshCore companion firmware runs one.

    Two settings, under the firmware's names and with its defaults: ``gps``, off until it is
    switched on and remembered across restarts, and ``gps_interval``, the seconds between
    position updates — ``0``, the default, takes every fix, which is the firmware's one a
    second. The receiver's NMEA is read on the node's own event loop (a few sentences a
    second is nothing beside the radio), and a valid fix is written into the node's
    preferences, which is where its self-info frame and its adverts both read the position
    from. Firmware keeps a fix in RAM only; here the position is saved with the node's other
    preferences whenever they are, and once more as the node shuts down, so a restart
    indoors begins from the last fix rather than from a position set by hand long ago.

    Attributes:
        port: The receiver's serial port.
        baud: Its line speed.
        enabled: Whether ``gps`` is on.
        interval_s: ``gps_interval``.
        node: The companion whose position a fix moves; set once it exists.
        moved: Whether a fix has moved the position since the preferences were last saved.
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
        """Whether the receiver's port is open and being read."""
        return self._fd is not None

    def custom_vars(self) -> dict[str, str]:
        """The two settings as the firmware reports them: ``gps`` says whether it *runs*."""
        return {"gps": "1" if self.running else "0", "gps_interval": str(self.interval_s)}

    def saved(self) -> dict[str, int]:
        """The two settings as ``prefs.json`` keeps them, under the firmware's field names."""
        return {"gps_enabled": int(self.enabled), "gps_interval": self.interval_s}

    def set_var(self, name: str, value: str) -> bool:
        """Set ``gps`` or ``gps_interval``; ``False`` refuses the value, changing nothing."""
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
            self._next_at = 0.0  # the new pace counts from the next fix
            return True
        return False

    def start(self) -> None:
        """Open the port raw at the receiver's speed and read it on the running loop.

        Raises:
            OSError: The port can't be opened or set up (missing, not ours, not a tty).
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
        """Stop reading and close the port; the position keeps the last fix."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(fd)
        except RuntimeError:  # no loop left to remove it from: closing is enough
            pass
        os.close(fd)
        log.info("GPS on %s stopped", self.port)

    def _readable(self) -> None:
        """Read what the port has; a port that hangs up or fails is stopped, not retried."""
        try:
            chunk = os.read(self._fd, 4096)  # type: ignore[arg-type]
        except BlockingIOError:
            return
        except OSError as exc:
            log.warning("GPS on %s failed: %s", self.port, exc)
            self.stop()
            return
        if not chunk:  # hung up: it would read as ready forever
            log.warning("GPS on %s hung up", self.port)
            self.stop()
            return
        self.feed(chunk)

    def feed(self, chunk: bytes, now: float | None = None) -> None:
        """Take bytes as they arrive and act on each whole sentence among them."""
        *lines, self._pending = (self._pending + chunk).split(b"\n")
        if len(self._pending) > _NMEA_MAX:
            self._pending = b""
        for raw in lines:
            fix = nmea_fix(raw.decode("ascii", "replace"))
            if fix is not None:
                self._take(fix, time.monotonic() if now is None else now)

    def _take(self, fix: tuple[float, float], now: float) -> None:
        """Make ``fix`` the node's position, once per interval."""
        if self.node is None or now < self._next_at:
            return
        if not self._fixed:  # once a start, and never where: a log is pasted into issues
            log.info("GPS has a fix")
            self._fixed = True
        prefs = self.node.prefs
        if (prefs.latitude, prefs.longitude) != fix:
            prefs.latitude, prefs.longitude = fix
            self.moved = True
        self._next_at = now + max(self.interval_s, 1)


def _persist_contacts(store: Any, path: Path) -> None:
    """Snapshot the contact list after every change, the way the firmware writes flash."""

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
    """Bring the node up, report ready, and serve until told to stop.

    Args:
        config: ``state_dir``, ``wiring`` (the board's pins and switches) and ``seed``
            (name and radio settings for a node with no ``prefs.json`` yet).
        report: Sends the one status line to the parent.

    Returns:
        The process exit status.
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

    # The board's switches go back as they were however the node ends — a radio that never
    # came up included — so a failed start leaves the header as it found it.
    switched = prepare_board(wiring)
    try:
        return await _serve(
            state,
            wiring,
            seed,
            report,
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
    models: Any,
    sx1262: Any,
    companion_mod: Any,
    identity_mod: Any,
) -> int:
    """Bring the radio up on a prepared board, report ready, and serve until told to stop."""
    import inspect

    # The saved preferences decide the radio the chip is brought up on; the seed only fills
    # in a node that has never saved any.
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
        except SystemExit:  # the GPIO manager's answer to a busy or forbidden pin
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
                except OSError as exc:  # the radio still works; `gps` reads 0 until it opens
                    log.warning("GPS on %s did not open: %s", gps.port, exc)
        public_key = node.get_public_key()
        server = companion_mod.CompanionFrameServer(
            bridge=node,
            companion_hash=f"{public_key[0]:02x}",
            port=0,
            bind_address="127.0.0.1",
            local_hash=public_key[0],
            device_model=DEVICE_MODEL,
            client_idle_timeout_sec=None,
        )
        await server.start()
        # The one push the library's frame server doesn't subscribe to: every overheard frame
        # with its SNR/RSSI, which MeshTerm's live feed is built from.
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
                node._save_prefs()  # the last fix, so a restart begins where the node was
        for step in (server.stop, node.stop):
            try:
                await step()
            except Exception as exc:  # noqa: BLE001 - keep going: the radio still needs freeing
                log.warning("shutdown step failed: %s", exc)
        node.contacts.save_snapshot()
        radio.cleanup()
    return 0


def apply_preamble(radio: Any, symbols: int) -> None:
    """Give a running radio a new preamble length, for both what it sends and what it hears.

    The driver reads ``preamble_length`` afresh for every transmission, but reception keeps
    the packet parameters it was last given, so those are re-sent from standby and the chip
    put back to listening.
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
    """``CompanionRadio`` with its preferences written to ``prefs.json`` on every change.

    ``_save_prefs`` is the library's own hook for exactly this ("subclasses that need
    persistence … should override this method"), called after every preference setter.
    With a ``gps``, the board's two GPS settings are among its custom variables, as the
    firmware lists them, and are saved beside the preferences the library knows.
    """

    class PersistentCompanion(base):  # type: ignore[misc, valid-type]
        def set_radio_params(self, freq_hz: int, bw_hz: int, sf: int, cr: int) -> bool:
            """Retune, and bring the preamble along when the spreading factor moves it.

            The library retunes the modulation but leaves the packet parameters alone, and
            the preamble is one of them (see :func:`preamble_for_sf`).
            """
            ok = super().set_radio_params(freq_hz, bw_hz, sf, cr)
            if ok:
                apply_preamble(self._radio, preamble_for_sf(sf))
            return ok

        def get_custom_vars(self) -> dict[str, str]:
            """The library's variables, and a GPS board's two as firmware reports them."""
            found = super().get_custom_vars()
            if gps is not None:
                found.update(gps.custom_vars())
            return found

        def set_custom_var(self, name: str, value: str) -> bool:
            """Set a variable; ``gps`` and ``gps_interval`` run the receiver and are saved."""
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
    """Wait for stdin to close or a SIGTERM/SIGINT — the parent's two ways of saying stop."""
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
    """On Linux, have the kernel send SIGTERM when the parent exits (best-effort)."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``python radionode.py --config '<json>'``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="the node's configuration, as JSON")
    args = parser.parse_args(argv)

    # One line on the real stdout is the parent's; everything else goes to stderr.
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
