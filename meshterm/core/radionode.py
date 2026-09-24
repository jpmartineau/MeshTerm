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
``prefs.json``     name, radio settings, TX power, position, the other prefs
``channels.json``  the channel table, slot by slot
``contacts.json``  the contact list
=================  =============================================================

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
import json
import logging
import os
import signal
import sys
import threading
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

    Kinds: ``runtime`` (the radio library is missing or too old), ``no-spi`` / ``no-gpio``
    (the device node doesn't exist), ``permission`` (it exists but this user can't open it),
    ``busy`` (another program holds the radio's pins), and ``failed`` (anything else).
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
    import inspect

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
    apply_saved_prefs(prefs, _read_json(state / "prefs.json"))

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
        node = _persistent_companion(companion_mod.CompanionRadio, state)(
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


def _persistent_companion(base: type, state: Path) -> type:
    """``CompanionRadio`` with its preferences written to ``prefs.json`` on every change.

    ``_save_prefs`` is the library's own hook for exactly this ("subclasses that need
    persistence … should override this method"), called after every preference setter.
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

        def _save_prefs(self) -> None:
            try:
                _write_json(state / "prefs.json", prefs_to_json(self.prefs))
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
