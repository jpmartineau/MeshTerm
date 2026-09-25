# SPDX-License-Identifier: Apache-2.0
"""Device connection abstraction.

Services and tools depend only on the :class:`Device` interface, never on the
``meshcore`` library directly. This keeps the algorithms testable and lets the
:class:`MockDevice` simulator stand in for real hardware during development.

Two implementations are provided:

* :class:`MeshCoreDevice` - wraps the async ``meshcore`` companion-protocol client.
* :class:`MockDevice` - a deterministic simulator with a physically plausible
  SNR-vs-TX-power response, used by ``--mock`` and the test suite.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import transmit_gate
from .channels import CHANNEL_SLOT_PROBE_CAP
from .events import MeshEvent
from .frames import frame_addressing, trace_link_snrs
from .models import (
    NODE_TYPE_CHAT,
    NODE_TYPE_REPEATER,
    Ack,
    Contact,
    Hop,
    LoginResult,
    Message,
    NeighbourInfo,
    Observation,
    TraceResult,
    advert_time,
    utcnow,
)
from .region_sim import SimulatedRegionMap
from .regions import WILDCARD as REGION_WILDCARD
from .regions import normalize as normalize_region
from .regions import parse_region_list, region_key
from .regions import validate as validate_region
from .tracing import path_hash_flags, trace_timeout
from .transmit_lock import TransmitLock

if TYPE_CHECKING:
    from .config import SpiWiring
    from .discovery import DiscoveredDevice

#: Module logger; enable DEBUG on ``meshterm.core.connection`` to trace the message pump.
_log = logging.getLogger(__name__)

#: Callback invoked with each :class:`~meshterm.core.events.MeshEvent` the device emits
#: (an overheard packet, an inbound message, an acknowledgement).
EventCallback = Callable[[MeshEvent], None]

#: Zero-argument callable returned by :meth:`Device.subscribe_events` that stops the
#: subscription and releases its resources when invoked.
Unsubscribe = Callable[[], None]

#: How often the :class:`MockDevice` simulator emits a fresh burst of synthetic packets
#: while a passive-monitor subscription is open (seconds).
_MOCK_MONITOR_INTERVAL_S = 0.05

#: How often the real device's inbound-message pump sweeps for queued messages, as a
#: safety net for firmware that doesn't reliably push ``MESSAGES_WAITING`` (seconds).
_MESSAGE_POLL_INTERVAL_S = 3.0

#: Standard Bluetooth GATT Battery Service and its Battery Level Status characteristic (GATT
#: Specification Supplement, "Battery Level Status", 0x2BED — added in Battery Service 1.1).
#: Where a companion exposes these, the status characteristic carries a *firmware-reported*
#: charging flag — a ground truth the MeshCore companion protocol itself never provides (it
#: reports only a battery voltage). MeshCore firmware does not implement them today: its BLE
#: profile is just the Nordic UART pipe plus DFU, confirmed by dumping the GATT table. So this
#: path lies dormant behind a fail-safe fallback and lights up automatically only if a future
#: device ships the service. See :func:`charging_from_battery_level_status`.
_BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
_BATTERY_LEVEL_STATUS_UUID = "00002bed-0000-1000-8000-00805f9b34fb"


def charging_from_battery_level_status(data: bytes) -> bool | None:
    """Decode the charging state from a GATT *Battery Level Status* value (0x2BED).

    Per the Bluetooth GATT Specification Supplement the characteristic opens with a 1-byte
    flags field followed by a 16-bit little-endian *Power State* word; the two bits at offset
    5 are the **Charge State** enum — 0 unknown, 1 charging, 2 discharging (active), 3
    discharging (inactive). The fields after the word (identifier, battery level, additional
    status) are optional and unread here, so only the first three bytes are required.

    Args:
        data: The raw characteristic value as read over GATT.

    Returns:
        ``True`` when the pack reports it is charging, ``False`` when it reports discharging,
        or ``None`` when the value is too short or the state is *unknown* — in which case the
        caller should fall back to the voltage-trend inference.
    """
    if len(data) < 3:
        return None
    power_state = int.from_bytes(data[1:3], "little")
    charge_state = (power_state >> 5) & 0b11
    if charge_state == 1:
        return True
    if charge_state in (2, 3):
        return False
    return None


#: Per-``get_msg`` timeout in the message pump, so a missing device reply can't wedge the
#: drain loop (seconds).
_MESSAGE_GET_TIMEOUT_S = 5.0

#: How many uncorrelated frames one :meth:`MeshCoreDevice.admin_login` may quote in its log
#: line. They are evidence for reading a failure afterwards, not a record to keep, and a busy
#: mesh can push a great many through the window — a handful names the fault, and the rest
#: would only bury the outcome they sit beside.
_LOGIN_STRAY_LOG_CAP = 4

#: How long a graceful ``meshcore`` client teardown may take before it is abandoned and the
#: transport is force-closed instead (seconds). A healthy disconnect completes in well under a
#: second; the bound exists because the library's dispatcher shutdown can deadlock — its
#: ``queue.join()`` never returns when two or more events (a routine serial RX burst) are
#: queued at the moment of stop, since the processor task exits after draining only one. Kept
#: comfortably under the interactive session's 5-second exit watchdog so even the forced path
#: finishes as a *clean* exit rather than an ``os._exit`` reap.
_DISCONNECT_TIMEOUT_S = 2.0

#: Bound on the forced transport close that follows an abandoned graceful teardown (seconds).
#: ``_DISCONNECT_TIMEOUT_S + _FORCE_DISCONNECT_TIMEOUT_S`` stays under the exit watchdog.
_FORCE_DISCONNECT_TIMEOUT_S = 1.5

#: Bound on the graceful half of closing a meshcore client whose own ``connect`` failed
#: (seconds). Deliberately short: this runs on a failure path the user is waiting through —
#: the startup probe that is about to raise "needs a PIN" — and a half-open client has no
#: session state worth draining. The forced transport close follows regardless.
_DISCARD_TIMEOUT_S = 2.0

#: Total attempts at opening the BLE link before its failure is surfaced. Opening a BLE
#: connection on Windows is intermittently flaky (a slow-advertising peripheral is missed by
#: bleak's internal lookup, or the link-layer connect races the just-finished discovery scan);
#: a single retry recovers the common case without meaningfully delaying a genuinely absent
#: device. This retries only the *link open* — never a rejected PIN and never a mesh transmit.
_BLE_CONNECT_ATTEMPTS = 2

#: Pause between BLE link-open attempts (seconds), giving the OS radio a beat to settle.
_BLE_CONNECT_RETRY_DELAY_S = 1.0

#: Attempts, and the wait between them, while macOS finishes a pairing it has just begun.
#: An unbonded subscribe to the companion's authenticated characteristic is what *triggers*
#: Passkey Entry there, so the very failure we catch is the signal that the OS has raised
#: its dialog — and the person now has to read a 6-digit code off the device and type it.
#: Giving up at once meant a correct PIN produced an error and only the *second* attempt
#: worked, the first having quietly done the bonding. Sized to cover a human typing a code
#: rather than a radio settling, and bounded so a cancelled dialog still fails in the end.
_BLE_MACOS_PAIRING_ATTEMPTS = 5
_BLE_MACOS_PAIRING_DELAY_S = 6.0

TX_POWER_MIN = 1
TX_POWER_MAX = 22

#: Default range explored when tuning a *remote* repeater's transmit power. Remote nodes
#: (e.g. high-gain repeaters) typically run hotter than the local companion, so this band
#: differs from the local ``TX_POWER_MIN``/``TX_POWER_MAX`` clamp. Both bounds are
#: user-configurable (see :class:`~meshterm.core.config.Settings`).
REMOTE_TX_MIN = 12
REMOTE_TX_MAX = 28


class DeviceCommandError(RuntimeError):
    """A device command failed in a recoverable, user-facing way.

    Raised for conditions worth reporting cleanly (no traceback) — chiefly the
    companion's intermittent failure to answer a query in time. Callers may retry.
    """


class ContactNotOnDeviceError(DeviceCommandError):
    """The companion has no contact matching the recipient, so it cannot address it.

    Firmware addresses a direct message by looking the recipient up in *its own* contact
    table (by a prefix of the public key) and answers ``ERR_CODE_NOT_FOUND`` when nothing
    matches — the one send rejection with an obvious fix: put the contact back on the
    device. A distinct :class:`DeviceCommandError` subclass so the chat screen can offer
    exactly that (see :func:`~meshterm.ui.chat.open_chat`) while every other caller keeps
    treating it as an ordinary command failure.

    This is reachable for a contact MeshTerm itself listed, because the contact list a
    screen sees is the union of the device's live table and the ones MeshTerm remembers
    for it (see :mod:`meshterm.core.contact_store`): a contact the firmware has since
    dropped still lists, and only the send finds out it is gone.

    :meth:`Device.remove_contact` raises it for the same lookup against the same table,
    where it means the opposite thing: nothing left to delete on the radio. A removal
    therefore *reports* it and carries on dropping the contact from what MeshTerm
    remembers, rather than failing and leaving a row the reader cannot get rid of.

    Attributes:
        contact: The recipient the device could not find.
    """

    def __init__(self, contact: Contact) -> None:
        """Explain the rejection in terms of the contact the device could not find.

        Args:
            contact: The recipient the companion has no entry for.
        """
        super().__init__(f"{contact.name} isn't in this device's contacts — add it back to send.")
        self.contact = contact


class DeviceAuthenticationError(DeviceCommandError):
    """A Bluetooth companion refused the connection because it needs a pairing PIN/bond.

    A distinct :class:`DeviceCommandError` subclass so callers can tell "this device needs a
    PIN" apart from an ordinary command failure and offer to collect one: the interactive
    picker opens a PIN dialog and retries, while the scripted CLI (which catches the base
    class) prints the message and bails, since it can't prompt. The message already names the
    fix (``--ble-pin`` and OS pairing).

    The one exception covers several different failures — no PIN at all, a stale bond the OS
    still believes in, a wrong PIN, a pairing the device itself refused — and each has its own
    remedy, so the message names which one happened (see
    :meth:`MeshCoreDevice._ble_auth_failure`) rather than one sentence covering all of them.

    Attributes:
        hint: The same diagnosis cut to one line for the PIN dialog, which re-asks under it;
            empty when there is nothing to say beyond the dialog's own question.
    """

    def __init__(self, message: str, *, hint: str = "") -> None:
        """Build the error.

        Args:
            message: The full, actionable sentence — what failed and what to do.
            hint: The dialog-sized version of it (see the class attributes).
        """
        super().__init__(message)
        self.hint = hint


class FloodScopeError(DeviceCommandError):
    """The companion could not send under the scope a channel send asked for — so it didn't.

    A scoped send is set-scope, send, restore (see :meth:`Device.send_channel_in_scope`),
    and when the first step fails the message is **not** sent at all: going out unscoped
    instead would reach every repeater the reader meant to keep it from, and going out
    under the device's default scope would reach a region they never picked. Either would
    be a silent substitute for what was asked. The message names the firmware each kind of
    scope needs, since an old companion is by far the likeliest reason.

    Attributes:
        scope: The scope that was asked for — a region name, or ``*`` for unscoped.
    """

    def __init__(self, scope: str, reason: str) -> None:
        """Explain which scope could not be set and why.

        Args:
            scope: The region name, or :data:`~meshterm.core.regions.WILDCARD`.
            reason: The sentence to show (it already says nothing was sent).
        """
        super().__init__(reason)
        self.scope = scope


#: The companion firmware that first takes a session scope (``CMD_SET_FLOOD_SCOPE``), and
#: the one that first takes the explicit-unscoped override on it (``*``, sent as flag byte
#: ``0x01``) — before that release the flag frame is not understood, so an "unscoped" send
#: would fall back to the default scope instead.
SCOPE_FIRMWARE = (1, 10)
UNSCOPED_FIRMWARE = (1, 16)

#: The firmware that first has a persisted default scope (``CMD_SET_DEFAULT_FLOOD_SCOPE``).
#: On anything older a plain flood is unscoped by construction.
DEFAULT_SCOPE_FIRMWARE = (1, 15)


def firmware_version(info: dict | None) -> tuple[int, int, int] | None:
    """The companion's release as a comparable tuple, read from its device-query ``ver``.

    Firmware reports its build as a string — ``v1.15.0``, ``1.16.0-dev``, a vendor's own
    suffix — and only the leading ``major.minor[.patch]`` means anything here.

    Args:
        info: A :meth:`Device.get_device_info` payload.

    Returns:
        ``(major, minor, patch)``, or ``None`` where nothing version-shaped was reported
        (the simulator, or firmware predating the device query) — a caller then trusts the
        command itself to say whether it is understood.
    """
    match = _VERSION.search(str((info or {}).get("ver") or ""))
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


#: The leading ``major.minor[.patch]`` of a firmware build string.
_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def _version_text(version: tuple[int, ...]) -> str:
    """``(1, 16)`` as ``1.16`` for a sentence."""
    return ".".join(str(part) for part in version[:2])


def _scope_refusal(scope: str, exc: BaseException) -> str:
    """The sentence for a companion that refused to set a send scope.

    Args:
        scope: The region name, or ``*``.
        exc: What the command raised, kept in the sentence for whoever is debugging it.

    Returns:
        A sentence naming the firmware the scope needs, and that nothing was sent.
    """
    if scope == REGION_WILDCARD:
        need = f"sending unscoped needs firmware {_version_text(UNSCOPED_FIRMWARE)} or newer"
    else:
        need = f"region scopes need firmware {_version_text(SCOPE_FIRMWARE)} or newer"
    return f"The radio refused scope {scope} ({exc}) — {need}. Nothing was sent."


#: ``ERR_CODE_NOT_FOUND`` — the companion has no entry matching what a command addressed.
_ERR_NOT_FOUND = 2

#: ``CMD_SET_AUTOADD_CONFIG`` — the auto-add bitmask, and (as an optional second byte, which
#: the ``meshcore`` library never sends) the hop limit.
_CMD_SET_AUTOADD_CONFIG = 58

#: Companion command that persists the default flood scope (firmware 1.15+), framed by
#: :meth:`MeshCoreDevice.set_default_flood_scope` itself rather than the library.
_CMD_SET_DEFAULT_FLOOD_SCOPE = 63

#: Companion command that sets (or clears, or forces off) the session flood scope
#: (firmware 1.10+; the force-unscoped mode 1.16+), framed by
#: :meth:`MeshCoreDevice.set_flood_scope` itself rather than the library.
_CMD_SET_FLOOD_SCOPE_KEY = 54

#: ``RESP_CODE_AUTOADD_CONFIG`` — the reply to a read of the auto-add configuration. Its
#: second payload byte is the hop limit, which the library's parser drops (it keeps only the
#: bitmask), so :meth:`MeshCoreDevice.get_autoadd_config` recognises the raw frame by this.
_RESP_AUTOADD_CONFIG = 25

#: Sentinel: no hop limit has been read alongside the last auto-add bitmask.
_UNREAD = object()

#: The client-repeat frequencies (MHz) the companion firmware allows when a board defines
#: none of its own (``repeat_freq_ranges`` in MeshCore's ``examples/companion_radio/MyMesh.cpp``).
_DEFAULT_REPEAT_FREQS: tuple[tuple[float, float], ...] = (
    (433.0, 433.0),
    (869.495, 869.495),
    (918.0, 918.0),
)

#: How far a radio frequency may sit from an allowed repeat range and still be inside it
#: (MHz). Frequencies travel in kHz, so half of one is the whole rounding error.
_REPEAT_FREQ_TOLERANCE_MHZ = 0.0005


def repeat_freq_allowed(freq: float, ranges: Iterable[tuple[float, float]]) -> bool:
    """Whether ``freq`` (MHz) lies in one of the firmware's client-repeat ``ranges``.

    The firmware's own ``isValidClientRepeatFreq``, in MHz: it refuses to turn relaying on
    anywhere else.
    """
    tol = _REPEAT_FREQ_TOLERANCE_MHZ
    return any(low - tol <= float(freq) <= high + tol for low, high in ranges)


#: What each companion error code means, in a sentence that finishes "the device …".
#: The wire carries only the number and the library's ``ERR_CODE_*`` spelling (see
#: ``meshcore.events.ErrorMessages``), which is diagnostic text, not something to put in
#: front of a user — :func:`reject_reason` turns it into the sentence below.
_ERROR_REASONS = {
    1: "the firmware doesn't support that command",
    2: "the device has no contact with that key",
    3: "the device's table is full",
    4: "the device isn't in a state to do that",
    5: "the device hit a storage error",
    6: "the device rejected the request as malformed",
}


def _clip_utf8(text: str, limit: int) -> str:
    """Trim ``text`` to at most ``limit`` UTF-8 bytes, never splitting a character.

    Firmware fields are byte-sized, not character-sized, so a name with an accent in it can
    overrun a field it looks short enough for.

    Args:
        text: The value to fit.
        limit: The field's size in bytes.

    Returns:
        ``text`` itself when it fits, else its longest whole-character prefix that does.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def error_code(result) -> int | None:  # noqa: ANN001
    """Return the companion error code carried by a rejected command's event, if any.

    Args:
        result: The :class:`meshcore.events.Event` a command returned (or ``None``).

    Returns:
        The ``error_code`` from the event's payload, or ``None`` when the rejection carried
        no code (an absent reply, or a payload shaped some other way).
    """
    payload = getattr(result, "payload", {}) or {}
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload["error_code"])
    except (KeyError, TypeError, ValueError):
        return None


def reject_reason(result) -> str:  # noqa: ANN001
    """Explain a rejected command in one clause, for the end of a user-facing sentence.

    A rejection reaches us as ``{'error_code': 2, 'code_string': 'ERR_CODE_NOT_FOUND'}``;
    pasting that dict into a status line (which is what every raise site used to do) shows
    the user a wire constant and leaves them no wiser. This maps the code to plain words,
    falling back to the raw payload only for a rejection with no code we know.

    Args:
        result: The :class:`meshcore.events.Event` a command returned (or ``None``).

    Returns:
        A lowercase clause naming the cause, e.g. ``"the device's table is full"``.
    """
    code = error_code(result)
    if code in _ERROR_REASONS:
        return _ERROR_REASONS[code]
    if result is None:
        return "the device didn't answer"
    payload = getattr(result, "payload", {}) or {}
    return f"the device rejected it ({payload})"


#: Exception class names that signal the link to the companion has dropped — the device was
#: unplugged, powered off, moved out of range, or its port/transport otherwise vanished — as
#: opposed to an ordinary command-level failure. Matched by name in :func:`is_connection_lost`
#: so the optional ``pyserial``/``bleak`` dependencies need not be imported here (neither is
#: installed on the ``--mock`` path). ``SerialException`` covers pyserial's read/write failures
#: (including the Windows ``ClearCommError``/``WriteFile`` variants); the ``Bleak*`` names cover
#: a Bluetooth link that dropped or a peripheral that went out of range; the ``OSError``
#: subclasses cover a link torn down at the OS layer.
_CONNECTION_LOST_TYPES = frozenset(
    {
        "SerialException",
        "PortNotOpenError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "BleakError",
        "BleakDeviceNotFoundError",
        "BleakDBusError",
        "BleakGATTError",
        "BleakCharacteristicNotFoundError",
    }
)

#: Lowercase message fragments that also indicate a dropped link, for exceptions raised as a
#: plain ``OSError``/``RuntimeError`` (whose type name alone isn't conclusive). Kept specific
#: enough not to fire on ordinary command timeouts.
_CONNECTION_LOST_HINTS = (
    "device disconnected",
    "device not configured",
    "clearcommerror",
    "the handle is invalid",
    "the device does not recognize the command",
    "readfile failed",
    "writefile failed",
    "port is closed",
    "no such device",
    "input/output error",
    # BLE link-loss phrasings (bleak errors / meshcore BLE transport callback reasons).
    "ble_transport_lost",
    "ble_write_failed",
    "ble_disconnect",
    "not connected to a ble device",
    "device is no longer connected",
)


def is_connection_lost(exc: BaseException) -> bool:
    """Return whether ``exc`` means the companion serial link has dropped.

    Distinguishes a *lost connection* (the device was unplugged, powered off, or its serial
    port vanished) from an ordinary command failure, so the interactive session can offer to
    reconnect rather than merely report an error. The whole exception chain
    (``__cause__``/``__context__``) is walked, matching by exception type name and message
    text — see :data:`_CONNECTION_LOST_TYPES` / :data:`_CONNECTION_LOST_HINTS` — so the
    optional ``pyserial`` dependency need not be imported here.

    Args:
        exc: The exception raised by a device operation.

    Returns:
        ``True`` if the exception (or any it was raised from) looks like a dropped link.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _CONNECTION_LOST_TYPES:
            return True
        text = str(current).lower()
        if any(hint in text for hint in _CONNECTION_LOST_HINTS):
            return True
        current = current.__cause__ or current.__context__
    return False


#: Message fragments a BLE stack uses when a characteristic can't be accessed without a bond —
#: i.e. the companion is PIN-protected and we're unpaired (or gave the wrong PIN). The GATT
#: subscribe fails with one of these rather than a dropped link, so they're handled as a
#: distinct, actionable "needs a PIN" case (see :func:`_is_ble_auth_error`) and never as
#: connection loss. Matched by text so ``bleak`` need not be imported here.
_BLE_AUTH_HINTS = (
    "insufficient authentication",
    "insufficient authorization",
    "insufficient encryption",
    "not paired",
    # The pairing step itself failing, rather than the subscribe it guards: bleak's
    # ``pair()`` ("Could not pair with device: …") and BlueZ's AuthenticationFailed. Without
    # these a refused pairing fell through as an anonymous failure and was reported as a
    # device that "didn't answer as a MeshCore device".
    "could not pair",
    "authentication failed",
    "authenticationfailed",
)


@dataclass(frozen=True)
class _BlePairing:
    """What the last Windows PIN-pairing attempt found, kept so a refusal can say why.

    The pairing step is best-effort and returns a bool (see
    :meth:`MeshCoreDevice._pair_ble_windows`), which is all the connect needs to decide
    whether to retry — but a bool can't tell the reader whether the PIN was wrong, the device
    refused, or Windows couldn't reach it, and those have different fixes.

    Attributes:
        outcome: ``"reused"`` (an existing bond was trusted), ``"paired"``, ``"absent"``
            (Windows couldn't reach the device), ``"failed"`` (the ceremony returned a
            non-success status), or ``"error"`` (the WinRT call raised).
        status: The ceremony's status name, lowercase with spaces (``"authentication
            failure"``), or the exception text for ``"error"``; empty otherwise.
    """

    outcome: str
    status: str = ""


#: Pairing statuses that mean the PIN itself was wrong.
_PAIRING_WRONG_PIN = frozenset({"authentication failure", "invalid ceremony data"})

#: Pairing statuses that mean the device (not the PIN) turned the pairing down — busy with
#: another host, holding an old bond, or simply not answering in time.
_PAIRING_REFUSED = frozenset(
    {
        "connection rejected",
        "too many connections",
        "remote device has association",
        "not ready to pair",
        "rejected by handler",
        "authentication timeout",
        "protection level could not be met",
    }
)


def _is_ble_auth_error(exc: BaseException) -> bool:
    """Return whether ``exc`` (or any it was raised from) is a BLE authentication rejection.

    Walks the whole ``__cause__``/``__context__`` chain matching :data:`_BLE_AUTH_HINTS`, so a
    ``BleakGATTProtocolError`` wrapped by the meshcore transport is still recognized.

    Args:
        exc: The exception raised while opening the Bluetooth connection.

    Returns:
        ``True`` if the failure is a missing/rejected pairing rather than a dropped link.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if any(hint in text for hint in _BLE_AUTH_HINTS):
            return True
        current = current.__cause__ or current.__context__
    return False


def serial_port_present(port: str) -> bool:
    """Return whether a serial port named ``port`` is currently enumerated by the OS.

    This is the primary liveness signal for a mid-session unplug: the ``meshcore`` client
    keeps serving cached data after the cable is pulled and never raises (verified on
    hardware — a command still "succeeds", merely returning ``None``), so a failed command
    can't be relied on to notice. The OS port list, by contrast, drops the device the moment
    it is removed. Enumerating ports only reads the OS device table; it never opens the port,
    so it is safe to poll against a companion another handle already holds open.

    Args:
        port: The serial port name the device was opened on (e.g. ``COM11`` or
            ``/dev/ttyUSB0``).

    Returns:
        ``True`` if a port by that exact name is present (or if presence can't be
        determined — pyserial missing or the query failed — so a mere lookup hiccup never
        raises a false "disconnected" alarm).
    """
    try:
        from serial.tools import list_ports
    except Exception:  # noqa: BLE001 - pyserial absent (e.g. --mock env); can't tell, assume up
        return True
    try:
        if any(info.device == port for info in list_ports.comports()):
            return True
    except Exception:  # noqa: BLE001 - an enumeration failure must not fake a disconnect
        return True
    # A soldered platform-bus UART (e.g. an SoC port like ``/dev/ttyS1`` on the Luckfox Lyra)
    # is never enumerated by pyserial's Linux ``comports()`` — yet its device node persists for
    # exactly as long as the port exists. A USB serial node, by contrast, is removed from the
    # filesystem the instant the cable is pulled. So an existing ``/dev`` character device is a
    # sound presence signal that never masks a real unplug (and stays Windows-safe: COM names
    # are not filesystem paths, so this branch is skipped there).
    try:
        import os
        import stat

        if port.startswith("/dev/") and os.path.exists(port):
            return stat.S_ISCHR(os.stat(port).st_mode)
    except Exception:  # noqa: BLE001 - a stat hiccup must not fake a disconnect
        return True
    return False


class Device(ABC):
    """Abstract companion device exposing the operations MeshTerm needs.

    Implementations manage their own connection lifecycle and translate raw protocol
    events into the domain models in :mod:`meshterm.core.models`.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection to the device. Idempotent."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection and release resources. Idempotent."""

    async def link_present(self) -> bool:
        """Return whether the underlying transport link is still up (best-effort).

        A cheap, *non-invasive* liveness probe — it never transmits and never opens a new
        handle — so the interactive session can poll it while the device is in use to notice
        a mid-session drop (a serial cable pulled, a companion powered off, a BLE peripheral
        out of range). Each transport implements it against the signal that actually reflects
        its link state (OS port enumeration for serial, the BLE client's connection flag for
        Bluetooth). It returns ``True`` whenever presence can't be determined, so a lookup
        hiccup never fakes a disconnect.

        Returns:
            ``True`` if the link appears up (or can't be checked), ``False`` if it is gone.
        """
        return True

    @abstractmethod
    async def get_self_info(self) -> dict:
        """Return identity and radio configuration of the connected device.

        Returns:
            A dict with at least ``name`` and, when available, ``tx_power`` and radio
            parameters (``freq``, ``bw``, ``sf``, ``cr``).
        """

    @abstractmethod
    async def get_device_info(self) -> dict:
        """Return the firmware's hardware/build identity for the connected device.

        This is a *different* protocol frame from :meth:`get_self_info`: where self-info
        reports the node's identity and radio tuning, this reports what the box actually is —
        a ``model`` string (e.g. ``"Seeed Tracker T1000-E"``, matching a MeshCore firmware
        ``variant``), plus firmware ``ver``/``fw_build``. It is the *only* place the vendor and
        model surface: BLE adverts carry no manufacturer data for these boards, most use a
        randomized address with no IEEE OUI to look up, and no GATT Device Information Service
        is exposed — so the model has to come from MeshCore's own application layer.

        Best-effort: firmware predating the device-query frame answers with an empty payload
        rather than erroring, so callers must treat a missing ``model`` as simply unknown.

        Returns:
            A dict with, when available, ``model``, ``ver``, and ``fw_build``; possibly empty.
        """

    @abstractmethod
    async def get_contacts(self) -> list[Contact]:
        """Return the device's known contacts.

        Returns:
            The list of :class:`Contact` records currently stored on the device.
        """

    @abstractmethod
    async def add_contact(self, node: Contact) -> None:
        """Add (or update) a contact in the device's contact table.

        The inverse of :meth:`remove_contact`, and the fix for a node MeshTerm knows but the
        firmware has forgotten: a direct message is addressed by the *device's* own contact
        entry, so a contact missing from its table can't be messaged at all
        (:class:`ContactNotOnDeviceError`) until it is written back. Everything the entry
        needs travels on the contact — its public key, name, type and last advertised
        position — and it is added with no learned route, so the first message floods
        exactly as it would for a freshly-heard node.

        Args:
            node: The contact to write; must carry a full public key.

        Raises:
            DeviceCommandError: If the contact carries no public key to address it by, or
                the device rejected the write (a full contact table, most often).
        """

    @abstractmethod
    async def remove_contact(self, node: Contact) -> None:
        """Delete a contact from the device's contact table.

        Addresses the contact by its public key, so it must carry one (a contact heard
        as an advert always does). The node stays a *node* — its reception history and any
        overheard traffic are untouched — it is only dropped from the device's list of
        added, messageable contacts.

        Args:
            node: The contact to remove.

        Raises:
            ContactNotOnDeviceError: If the device holds no contact with that key — which
                is not a failed removal but a removal with nothing left to do, so callers
                drop it from what MeshTerm remembers and say so.
            DeviceCommandError: If the contact carries no public key to address it by, or
                the device rejected the removal for any other reason.
        """

    @abstractmethod
    async def get_tx_power(self) -> int | None:
        """Return the current TX power level, or ``None`` if unknown."""

    @abstractmethod
    async def set_tx_power(self, value: int) -> None:
        """Set the radio transmit power.

        Args:
            value: TX power level, clamped by the caller to the device's valid range.
        """

    # -- remote administration (tuning a node we have admin rights on) -----------

    @abstractmethod
    async def admin_login(self, node: Contact, password: str) -> LoginResult:
        """Authenticate as administrator on a remote node.

        Args:
            node: The contact to log in to. Its ``public_key`` addresses the node.
            password: The node's admin password.

        Returns:
            A :class:`LoginResult`. It is truthy only for
            :attr:`~LoginResult.ACCEPTED`, so a caller that just needs "am I in?" can
            still write ``if not await device.admin_login(...)``; one that acts on the
            failure must tell :attr:`~LoginResult.REFUSED` (the node said no — the
            password is wrong) from :attr:`~LoginResult.NO_REPLY` (nothing came back —
            the password is unproven, not disproven).
        """

    @abstractmethod
    async def send_remote_command(
        self, node: Contact, command: str, *, timeout: float = 8.0
    ) -> str | None:
        """Send one CLI command to a logged-in remote node and await its text reply.

        The generic remote-administration primitive: repeaters and room servers are
        configured through their text CLI (``get``/``set``/``advert``/…) carried as
        admin messages, and every higher-level remote operation is a spelling of this.
        Requires an authenticated session (:meth:`admin_login` first) — firmware
        silently ignores commands from strangers, which surfaces as a ``None`` reply.

        Args:
            node: The remote contact (must already be logged in).
            command: The CLI command text, e.g. ``"set txdelay 5"``.
            timeout: Seconds to wait for the node's reply.

        Returns:
            The reply text, or ``None`` if the node did not answer in time.

        Raises:
            DeviceCommandError: If the companion rejected the send outright.
        """

    @abstractmethod
    async def get_remote_tx_power(self, node: Contact) -> int | None:
        """Read a remote (admin) node's current transmit power.

        Args:
            node: The contact to query (must already be logged in).

        Returns:
            The node's TX power in dBm, or ``None`` if it could not be read.
        """

    @abstractmethod
    async def set_remote_tx_power(self, node: Contact, value: int) -> None:
        """Set a remote (admin) node's transmit power.

        Args:
            node: The contact to adjust (must already be logged in).
            value: TX power in dBm.

        Raises:
            DeviceCommandError: If the node rejected the command.
        """

    @abstractmethod
    async def fetch_neighbours(self, node: Contact) -> list[NeighbourInfo]:
        """Ask a remote node for its neighbour table: who it hears directly, and how well.

        A second vantage point for the topology graph: every entry is a link
        ``node ↔ neighbour`` with SNR measured *at the remote node*, including nodes we
        have never received anything from ourselves. Requires an authenticated session
        (:meth:`admin_login` first) — firmware silently ignores the request from guests,
        which surfaces here as a timeout.

        Args:
            node: The contact to query (must already be logged in).

        Returns:
            The reported neighbour entries; empty when the node's table is empty (a
            normal answer — repeaters forget neighbours across reboots and only relearn
            them as adverts arrive).

        Raises:
            DeviceCommandError: If the node never answered (not logged in, out of
                reach, or firmware without neighbour tables).
        """

    @abstractmethod
    async def request_regions(self, node: Contact) -> list[str]:
        """Ask a repeater which regions it relays floods for (firmware 1.12+).

        An anonymous request — no login — that a repeater answers only when it arrives
        *direct or zero-hop*: a flooded copy is dropped (firmware gates ``REGIONS`` behind
        ``isRouteDirect()``), so the library pins a routeless contact to zero hops for the
        exchange. In practice that means a neighbour, or a contact with a learned route.
        It is also rate-limited on the repeater, so this is a one-shot question, never a
        poll.

        Args:
            node: The repeater to ask.

        Returns:
            The region names it listed, bare, in its order — ``*`` first when it also
            relays unscoped floods (see :func:`~meshterm.core.regions.parse_region_list`).

        Raises:
            DeviceCommandError: If the repeater never answered (out of direct reach, too
                old, rate-limited, or not a repeater).
        """

    @abstractmethod
    async def run_trace(
        self,
        target: str,
        *,
        path: str | None = None,
        timeout: float | None = None,
    ) -> TraceResult:
        """Run a single path trace to ``target`` and return per-hop SNR.

        Args:
            target: Name or key prefix of the destination node.
            path: Optional explicit path to force, as a comma-separated string of
                single-byte hex key prefixes (e.g. ``"3d,f2,3d"``). When ``None``
                the connection resolves one from the contact's learned route (the
                firmware itself never routes a trace — an explicit path is all it
                walks), falling back to path-less only for unknown targets.
            timeout: Seconds to wait for the trace reply. ``None`` (the default) sizes the
                wait to the route: a trace has to travel the whole path out and back, so a
                long walk is given proportionally longer to come home
                (:func:`~meshterm.core.tracing.trace_timeout`).

        Returns:
            A :class:`TraceResult`; ``success`` is ``False`` on timeout.
        """

    # -- passive event stream ----------------------------------------------------

    @abstractmethod
    async def subscribe_events(self, on_event: EventCallback) -> Unsubscribe:
        """Begin streaming the device's unsolicited inbound events, without blocking.

        Subscribes to everything the companion surfaces on its own: overheard adverts and
        telemetry (as :attr:`~meshterm.core.events.EventKind.OBSERVATION` events), inbound
        direct/channel text messages (:attr:`~meshterm.core.events.EventKind.MESSAGE`),
        and delivery acknowledgements (:attr:`~meshterm.core.events.EventKind.ACK`). Each
        is delivered to ``on_event`` as a :class:`~meshterm.core.events.MeshEvent` as it
        arrives. Delivery continues in the background until the returned callable is
        invoked to stop it; the radio is never asked to transmit, it only listens. This is
        the primitive the always-on :class:`~meshterm.services.event_hub.EventHub` is
        built on.

        Note:
            This carries only *unsolicited* events. Replies correlated to a request we
            sent (a trace's ``TRACE_DATA``, a login result) are awaited by the issuing
            command instead, so those flows work with or without a live subscription.

        Args:
            on_event: Callback invoked with each :class:`MeshEvent` as it is heard.

        Returns:
            A zero-argument callable that stops the stream and releases the subscription.
        """

    # -- messaging ---------------------------------------------------------------

    @abstractmethod
    async def send_direct_message(self, contact: Contact, text: str) -> Ack | None:
        """Send a direct text message to a contact.

        Args:
            contact: The recipient; its ``public_key`` addresses the message.
            text: The message body.

        Returns:
            The delivery :class:`Ack` if one arrived before the send timed out, else
            ``None`` (the message was handed to the radio but not yet acknowledged).

        Raises:
            DeviceCommandError: If the companion rejected the send outright.
        """

    @abstractmethod
    async def send_channel_message(self, index: int, text: str) -> None:
        """Broadcast a text message on a channel slot.

        Args:
            index: Zero-based channel slot to transmit on.
            text: The message body.

        Raises:
            DeviceCommandError: If the companion rejected the send.
        """

    # -- transmitting under a scope -----------------------------------------------------

    @property
    def transmit_lock(self) -> TransmitLock:
        """The lock every transmission holds while it hands its frame to the companion.

        Created on first use rather than in ``__init__`` so an implementation need not
        remember to chain up for it. See :mod:`~meshterm.core.transmit_lock` for why it
        exists: a channel scope is a window on the companion's one session scope, and
        nothing else may be sent through that window.
        """
        lock = getattr(self, "_transmit_lock", None)
        if lock is None:
            lock = TransmitLock()
            self._transmit_lock = lock
        return lock

    @asynccontextmanager
    async def transmitting(self) -> AsyncIterator[None]:
        """Hold the transmit lock for one hand-over, repairing a scope a failed restore left.

        Every send an implementation makes that could go out as a flood takes this around
        the command that transmits — only that command: an ack or reply *wait* afterwards
        is not a transmission and must not hold a scoped channel send up behind it.

        A scoped send whose restore failed (see :meth:`send_channel_in_scope`) leaves the
        companion's session scope pointing at a channel's region, where every later flood
        would follow it. The first transmission to come through here afterwards puts it
        back before it sends anything — and if the companion still won't take the reset,
        the send goes ahead under the scope it had rather than not at all, logged, since
        refusing every transmission from then on would be the worse failure.
        """
        async with self.transmit_lock.held():
            leaked = getattr(self, "_scope_leaked", None)
            if leaked is not None and not getattr(self, "_scope_active", False):
                try:
                    await self.set_flood_scope(None)
                except Exception as exc:  # noqa: BLE001 - see the docstring: send regardless
                    _log.warning("send scope %r is still set on the radio: %s", leaked, exc)
                else:
                    self._scope_leaked = None
            yield

    async def send_channel_in_scope(self, index: int, text: str, scope: str | None) -> None:
        """Broadcast on a channel under a given scope, and leave the session scope as found.

        The one way MeshTerm sends a channel message. ``None`` is the plain send, going out
        under whatever the companion's default scope is; a region name or ``*`` is the
        three-step window the firmware makes a per-channel scope out of — set the session
        scope, send, restore it (``None``, back to the default) — all under
        :attr:`transmit_lock`, so no other flood can go out between the steps.

        What is refused is refused *before* anything is sent (:class:`FloodScopeError`):
        a scoped message never quietly goes out unscoped, or under the default scope.

        * A region name needs firmware :data:`SCOPE_FIRMWARE`; older firmware answers the
          command with an error, which is taken as the refusal it is.
        * ``*`` needs firmware :data:`UNSCOPED_FIRMWARE` to override a default scope.
          Older firmware can still send unscoped when there is no default to override —
          anything before :data:`DEFAULT_SCOPE_FIRMWARE` has none at all, and later ones
          may have none set — so then the plain send *is* the unscoped one and goes out;
          only a default that is actually set is refused.

        Args:
            index: Zero-based channel slot to transmit on.
            text: The message body.
            scope: A region name, :data:`~meshterm.core.regions.WILDCARD` for unscoped, or
                ``None`` for the device's default.

        Raises:
            FloodScopeError: If the companion can't send under ``scope``; nothing was sent.
            ~meshterm.core.regions.RegionNameError: If ``scope`` is not a region name the
                firmware could hold.
            DeviceCommandError: If the send itself was rejected.
        """
        bare = normalize_region(scope) if scope else ""
        if bare and bare != REGION_WILDCARD:
            bare = validate_region(bare)
        async with self.transmitting():
            if not bare:
                await self.send_channel_message(index, text)
                return
            if bare == REGION_WILDCARD and await self._unscoped_is_plain():
                await self.send_channel_message(index, text)
                return
            try:
                await self.set_flood_scope(bare)
            except DeviceCommandError as exc:
                raise FloodScopeError(bare, _scope_refusal(bare, exc)) from exc
            self._scope_active = True
            try:
                await self.send_channel_message(index, text)
            finally:
                self._scope_active = False
                try:
                    await self.set_flood_scope(None)
                except Exception as exc:  # noqa: BLE001 - the message went; repair next time
                    _log.warning("couldn't clear send scope %r after a channel send: %s", bare, exc)
                    self._scope_leaked = bare

    async def _unscoped_is_plain(self) -> bool:
        """Whether a plain flood is already unscoped, so ``*`` needs no override — or refuse.

        Only firmware too old to take the override is asked anything; firmware that reports
        no version is trusted to answer the override command itself.

        Returns:
            ``True`` when the plain send is unscoped; ``False`` when the override is
            available and should be used.

        Raises:
            FloodScopeError: When a default scope is set and the firmware cannot override it.
        """
        try:
            version = firmware_version(await self.get_device_info())
        except Exception:  # noqa: BLE001 - an unreadable version is an unknown one
            version = None
        if version is None or version >= UNSCOPED_FIRMWARE:
            return False
        if version < DEFAULT_SCOPE_FIRMWARE:
            return True
        try:
            default = await self.get_default_flood_scope()
        except Exception:  # noqa: BLE001 - can't see the default: can't promise unscoped
            default = "?"
        if not normalize_region(default or ""):
            return True
        raise FloodScopeError(
            REGION_WILDCARD,
            f"This radio floods everything under its default scope {default}, and only "
            f"firmware {_version_text(UNSCOPED_FIRMWARE)} or newer can send one message "
            "unscoped over it. Nothing was sent.",
        )

    # -- configuration: extra reads ---------------------------------------------

    @abstractmethod
    async def get_tuning(self) -> dict:
        """Return radio tuning parameters, in their real units.

        Returns:
            A dict with ``rx_delay`` (float seconds) and ``airtime_factor`` (float).
            The firmware stores both as floats and moves them over the wire scaled
            ×1000; implementations undo that scaling so callers only ever see the
            real values.
        """

    @abstractmethod
    async def get_autoadd_config(self) -> int | None:
        """Return the contact auto-add bitmask, or ``None`` if the firmware predates it.

        The finer-grained sibling of :meth:`set_manual_add_contacts`: a bitmask of which
        advert types the firmware adds to contacts automatically.
        """

    async def get_autoadd_max_hops(self) -> int | None:
        """Return the auto-add hop limit, or ``None`` where the firmware doesn't report one.

        The second byte of the firmware's auto-add configuration (``autoadd_max_hops``,
        capped at 64). Not abstract: a device without the field has nothing to say, and the
        snapshot leaves its row unread rather than failing.
        """
        return None

    async def get_allowed_repeat_freqs(self) -> list[tuple[float, float]]:
        """Return the frequency ranges (MHz, inclusive) client repeat may be enabled on.

        The firmware refuses to relay outside them (``isValidClientRepeatFreq``), so the
        editor can check before staging instead of after a refusal. Empty where the firmware
        doesn't say — which means *unknown*, not *nowhere*.
        """
        return []

    @abstractmethod
    async def get_default_flood_scope(self) -> str | None:
        """Return the persisted default flood scope's name (``""`` when unset).

        Returns:
            The ``#scope`` name limiting flood routing, an empty string when no scope is
            configured, or ``None`` if the firmware predates flood scopes.
        """

    @abstractmethod
    async def get_time(self) -> int | None:
        """Return the device clock as a UNIX epoch timestamp, or ``None`` if unknown."""

    @abstractmethod
    async def get_battery(self) -> dict:
        """Return battery (and, when reported, storage) status.

        Returns:
            A dict with ``level`` (millivolts) and, on firmware that reports storage,
            ``used_kb``/``total_kb``. Empty when the read is unsupported.
        """

    async def get_hw_charging(self) -> bool | None:
        """Return a firmware-reported charging flag, or ``None`` when the device has none.

        The companion protocol carries only a battery voltage, so almost every device answers
        ``None`` and callers fall back to inferring charge from the voltage trend (see
        :meth:`~meshterm.services.battery_service.BatteryService._charging`). A transport that
        can read a real charging flag — a BLE device exposing the standard Battery Level Status
        characteristic (0x2BED) — overrides this to return it. Best-effort by contract: it
        never raises and never meaningfully blocks, so a caller may await it every poll.
        """
        return None

    @abstractmethod
    async def get_stats(self) -> dict:
        """Return the firmware's core/radio/packet statistics, merged into one dict.

        Each of the three stats frames is fetched best-effort — firmware predating one
        simply contributes nothing — so callers get whatever subset exists: ``battery_mv``,
        ``uptime_secs``, ``errors``, ``queue_len`` (core); ``noise_floor``, ``last_rssi``,
        ``last_snr``, ``tx_air_secs``, ``rx_air_secs`` (radio); ``recv``, ``sent``,
        ``flood_tx``, ``direct_tx``, ``flood_rx``, ``direct_rx``, ``recv_errors`` (packets).
        """

    @abstractmethod
    async def get_path_hash_mode(self) -> int:
        """Return the current path-hash mode (experimental routing flag)."""

    @abstractmethod
    async def get_custom_vars(self) -> dict[str, str]:
        """Return the device's experimental custom key/value variables."""

    @abstractmethod
    async def get_channel(self, index: int) -> dict | None:
        """Return one channel's configuration, or ``None`` if unset.

        Args:
            index: Zero-based channel slot.

        Returns:
            A dict with ``channel_idx``, ``channel_name`` and ``channel_secret``
            (16 raw bytes), or ``None`` when the slot is empty.
        """

    async def channel_capacity(self) -> int:
        """Discover how many channel slots this device exposes (read-only, non-destructive).

        Reads slots from 0 upward until the firmware rejects an index. An *empty* slot is a
        valid index and returns ``None`` without stopping the scan; only an out-of-range index
        makes the firmware answer with an error, which surfaces here as an exception. The scan
        is bounded by :data:`CHANNEL_SLOT_PROBE_CAP` so a device that never rejects an index
        can't loop forever — in that case the cap itself is reported.

        This only ever *reads* channel configuration, so it is safe to call against a live
        device without disturbing its state.

        Returns:
            The number of addressable channel slots the firmware was built with.
        """
        count = 0
        for idx in range(CHANNEL_SLOT_PROBE_CAP):
            try:
                await self.get_channel(idx)
            except Exception:  # noqa: BLE001 - a rejected index is how firmware signals its ceiling
                break
            count = idx + 1
        return count

    # -- configuration: settable values -----------------------------------------

    @abstractmethod
    async def set_name(self, name: str) -> None:
        """Set the node's advertised name."""

    @abstractmethod
    async def set_coords(self, lat: float, lon: float) -> None:
        """Set the node's advertised latitude/longitude (decimal degrees)."""

    @abstractmethod
    async def set_device_pin(self, pin: int) -> None:
        """Set the device's BLE pairing PIN."""

    @abstractmethod
    async def set_radio(
        self, freq: float, bw: float, sf: int, cr: int, repeat: bool | None = None
    ) -> None:
        """Set the core radio parameters.

        Args:
            freq: Frequency in MHz.
            bw: Bandwidth in kHz.
            sf: Spreading factor.
            cr: Coding rate denominator (``5``-``8`` for 4/5-4/8).
            repeat: Whether the companion relays mesh traffic (client repeat, firmware v9+).
                The firmware takes it as an optional trailing byte of this same command and
                reads the byte's *absence* as off — so a caller changing any radio field on
                firmware that reports it must restate it, or the change quietly stops the
                relaying. ``None`` omits the byte, for firmware that predates it.
        """

    @abstractmethod
    async def set_tuning(self, rx_delay: float, airtime_factor: float) -> None:
        """Set radio tuning parameters, in their real units.

        The firmware takes both fields in one command, so a caller changing one must
        resend the other. Values are the real ones (``rx_delay`` in seconds, 0–20;
        ``airtime_factor`` a duty-cycle factor, 0–9); implementations apply the
        protocol's ×1000 wire scaling. (The repeater-side TX delay factors are *not*
        part of this command — companion firmware reads exactly these two fields and
        ignores anything after them; those knobs are remote-CLI settings on repeaters.)
        """

    @abstractmethod
    async def set_manual_add_contacts(self, enabled: bool) -> None:
        """Set whether contacts must be added manually rather than automatically."""

    @abstractmethod
    async def set_adv_loc_policy(self, policy: int) -> None:
        """Set the advert location-sharing policy."""

    @abstractmethod
    async def set_multi_acks(self, value: int) -> None:
        """Set the multi-ack behavior flag."""

    @abstractmethod
    async def set_telemetry_modes(self, base: int, loc: int, env: int) -> None:
        """Set the three telemetry mode fields together (each ``0``-``3``)."""

    @abstractmethod
    async def set_autoadd_config(self, flags: int, max_hops: int | None = None) -> None:
        """Set the contact auto-add bitmask (see :meth:`get_autoadd_config`), and its hop limit.

        The firmware reads the hop limit as an optional second byte and leaves it where it
        was when that byte is absent, so ``None`` changes the bitmask alone.
        """

    @abstractmethod
    async def set_default_flood_scope(self, scope: str) -> None:
        """Persist the default flood scope by name (empty string clears it).

        The name is stored bare, as the firmware and the official apps store it; the
        ``#`` belongs only to deriving its 16-byte key (see :mod:`~meshterm.core.regions`).
        """

    @abstractmethod
    async def set_flood_scope(self, region: str | None) -> None:
        """Set the session send scope — what the next floods go out under, until changed.

        The firmware's session override (``CMD_SET_FLOOD_SCOPE_KEY``): not persisted, reset
        at boot, and applied to *every* flood the companion sends — channel messages, flood
        DMs, acks, path returns, requests — ahead of the persisted default. There is no
        per-channel scope in firmware, so a channel scope is this call made just before the
        channel send (and undone after it).

        Args:
            region: A region name to scope to; :data:`~meshterm.core.regions.WILDCARD`
                (``*``) to force unscoped even over a default scope (firmware 1.16+); or
                ``None`` to drop the override and fall back to the default scope.
        """

    @abstractmethod
    async def set_path_hash_mode(self, mode: int) -> None:
        """Set the experimental path-hash routing mode."""

    @abstractmethod
    async def set_custom_var(self, key: str, value: str) -> None:
        """Set an experimental custom variable."""

    @abstractmethod
    async def set_channel(self, index: int, name: str, secret: bytes | None) -> None:
        """Configure a channel slot.

        Args:
            index: Zero-based channel slot.
            name: Channel name (a leading ``#`` derives the secret from the name).
            secret: 16-byte shared secret, or ``None`` to derive it from ``name``.
        """

    # -- configuration: actions -------------------------------------------------

    @abstractmethod
    async def set_time(self, epoch: int) -> None:
        """Set the device clock to a UNIX epoch timestamp."""

    @abstractmethod
    async def send_advert(self, flood: bool = False) -> None:
        """Broadcast an advertisement (``flood`` propagates it across the mesh)."""

    @abstractmethod
    async def reboot(self) -> None:
        """Reboot the device."""

    @abstractmethod
    async def export_private_key(self) -> str:
        """Export the device's private key as a hex string (sensitive)."""

    @abstractmethod
    async def import_private_key(self, key_hex: str) -> None:
        """Import a private key from a hex string (overwrites the device identity)."""

    @abstractmethod
    async def factory_reset(self) -> None:
        """Erase all device data and restore factory defaults (destructive)."""

    async def __aenter__(self) -> Device:
        """Enter the async context manager, connecting the device."""
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the async context manager, disconnecting the device."""
        await self.disconnect()


class MeshCoreDevice(Device):
    """A :class:`Device` backed by the ``meshcore`` companion client (serial, BLE, or TCP).

    The same wrapper serves every transport: which one it opens is chosen by ``transport``
    (``"serial"`` opens ``port``; ``"ble"`` opens ``address``; ``"tcp"`` opens
    ``host``:``tcp_port``). Everything above the connection — commands, event mapping, trace
    parsing — is transport-agnostic, so only :meth:`connect` and :meth:`link_present` differ
    between them.

    Note:
        The trace mapping here follows the documented companion protocol (trace replies
        carry per-hop SNR encoded as ``SNR * 4``) but should be validated against your
        firmware version, as event payload field names vary between releases.
    """

    def __init__(
        self,
        port: str | None = None,
        baudrate: int = 115200,
        connect_timeout: float | None = None,
        *,
        transport: str = "serial",
        address: str | None = None,
        pin: str | None = None,
        ble_device: object | None = None,
        host: str | None = None,
        tcp_port: int | None = None,
    ) -> None:
        """Initialize the device wrapper.

        Args:
            port: Serial port path (e.g. ``COM5`` or ``/dev/ttyUSB0``); serial transport only.
            baudrate: Serial baud rate.
            connect_timeout: Handshake timeout (seconds) for the initial connection, passed
                to the client as its default command timeout. ``None`` uses the ``meshcore``
                library default (~15s). The startup smoke test sets a short value so a
                non-responsive port is rejected quickly instead of blocking on the full
                default handshake window.
            transport: ``"serial"`` (default), ``"ble"``, or ``"tcp"``.
            address: Bluetooth address (e.g. ``AA:BB:CC:DD:EE:FF``); BLE transport only.
            pin: Optional BLE pairing PIN, when the peripheral requires one (BLE only).
            ble_device: The ``bleak.BLEDevice`` the discovery scan already found at
                ``address``, when available (BLE only). Passing it lets the connect open the
                peripheral directly instead of re-discovering it by address — on Windows a
                bare-address connect runs a fresh internal scan that intermittently misses a
                slow-advertising companion, which is the main source of flaky BLE startups.
                Typed ``object`` so ``bleak`` need not be imported on non-BLE paths.
            host: Hostname or IP of a network companion; TCP transport only.
            tcp_port: TCP port the network companion listens on; TCP transport only.
        """
        self._port = port
        self._baudrate = baudrate
        self._connect_timeout = connect_timeout
        self._transport = transport
        self._address = address
        self._pin = pin
        #: What the last Windows PIN-pairing attempt found (``None`` when none ran), so an
        #: authentication refusal can name the failure rather than guess at it.
        self._ble_pairing: _BlePairing | None = None
        self._ble_device = ble_device
        self._host = host
        self._tcp_port = tcp_port
        self._mc = None  # type: ignore[var-annotated]  # meshcore.MeshCore
        #: The ``bleak.BleakClient`` behind a BLE link, kept because ``meshcore`` lets go of
        #: its own reference the moment the peripheral vanishes — see :meth:`_release_ble_client`
        #: for why nothing else can close it, and what that costs when it stays open.
        self._ble_client = None  # type: ignore[var-annotated]  # bleak.BleakClient
        # Serializes channel reads. The meshcore library's get_channel waits for "the next
        # CHANNEL_INFO event" with no correlation to the index it asked for, and the dispatcher
        # fans that event to *every* in-flight waiter — so two concurrent reads both resolve on
        # the first response and one caller silently gets the other's channel. Holding this lock
        # keeps at most one channel read outstanding, so the response is unambiguously ours.
        self._channel_read_lock = asyncio.Lock()
        #: Set once we've noted a device exposing the standard BLE Battery Service, so the
        #: "using its charging flag" log fires a single time per session rather than each poll.
        self._logged_bas = False

    @property
    def transport(self) -> str:
        """Which transport this device connects over (``"serial"``, ``"ble"``, or ``"tcp"``)."""
        return self._transport

    @property
    def endpoint(self) -> str | None:
        """The connection endpoint: ``host:port`` for TCP, the BLE address, else the port."""
        if self._transport == "tcp":
            return f"{self._host}:{self._tcp_port}"
        return self._address if self._transport == "ble" else self._port

    async def connect(self) -> None:  # noqa: D102 - inherited docstring
        if self._mc is not None:
            return
        try:
            from meshcore import MeshCore  # lazy import so --mock needs no hardware deps
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "The 'meshcore' library is required to talk to real hardware but is not "
                "installed. Run `pip install -e .` (or `pip install meshcore`), or use "
                "--mock for the simulator."
            ) from exc

        if self._transport == "ble":
            self._mc = await self._create_ble(MeshCore)
        elif self._transport == "tcp":
            try:
                self._mc = await MeshCore.create_tcp(
                    self._host, self._tcp_port, default_timeout=self._connect_timeout
                )
            except (OSError, asyncio.TimeoutError) as exc:
                # The socket couldn't be opened — host unreachable, connection refused, name
                # not resolved, or the connect timed out. That's not a MeshCore-level failure,
                # so translate it into a clean, actionable message (naming the host:port and
                # the things to check) rather than letting a raw socket traceback escape.
                raise DeviceCommandError(self._no_response_message()) from exc
        else:
            self._mc = await MeshCore.create_serial(
                self._port, self._baudrate, default_timeout=self._connect_timeout
            )
        # ``create_*`` returns ``None`` (after cleaning up its own connection) when the node
        # never answers the identity handshake — i.e. the endpoint isn't a MeshCore companion.
        # Surface that as a clean, recoverable error rather than leaving a half-open device
        # whose next command fails with a confusing "not connected".
        if self._mc is None:
            raise DeviceCommandError(self._no_response_message())
        # A short ``connect_timeout`` is only meant to bound the initial identity handshake
        # (so a dead endpoint is rejected quickly). Now that we're connected, restore the
        # library's normal per-command timeout so the rest of the session isn't rushed.
        if self._connect_timeout is not None:
            commands = getattr(self._mc, "commands", None)
            default = getattr(commands, "DEFAULT_TIMEOUT", None)
            if commands is not None and default is not None:
                commands.default_timeout = default

    async def _create_ble(self, mesh_core):  # type: ignore[no-untyped-def]
        """Open the BLE companion connection, pairing with a PIN first on Windows.

        ``auto_reconnect`` is deliberately left off: MeshTerm drives reconnection itself (the
        same reconnect dialog the serial path uses), so the meshcore client should surface a
        dropped link promptly via ``is_connected`` rather than silently retrying underneath us.

        Before opening the link we establish an *authenticated* pairing ourselves when a PIN is
        supplied (see :meth:`_pair_ble_windows`). This is essential on Windows: bleak's
        ``pair()`` only performs the "Just Works" ceremony (``CONFIRM_ONLY``) and never enters a
        passkey, so a PIN-protected companion bonds *without authentication* and then rejects
        the GATT subscribe on its authenticated UART characteristic — a correct PIN is reported
        as "rejected" and the device can never connect. Running the WinRT ProvidePin ceremony
        ourselves creates the authenticated bond the characteristic requires; once bonded, the
        OS keeps the bond and later reconnects need no PIN at all. The step is a harmless no-op
        on other platforms, when no PIN is set, or when the device is already bonded.

        Args:
            mesh_core: The imported ``meshcore.MeshCore`` class.

        Returns:
            The connected ``MeshCore`` client, or ``None`` if the peripheral never answered.
        """
        await self._pair_ble_windows(force=False)
        return await self._open_ble(mesh_core, allow_repair=True)

    async def _open_ble(self, mesh_core, *, allow_repair: bool):  # type: ignore[no-untyped-def]
        """Open the meshcore BLE client, translating auth failures and healing stale bonds.

        Args:
            mesh_core: The imported ``meshcore.MeshCore`` class.
            allow_repair: Whether a GATT authentication failure may trigger one unpair-and-
                re-pair-with-PIN retry (Windows only). Set ``False`` on that retry so a genuine
                wrong-PIN can't loop.

        Returns:
            The connected ``MeshCore`` client, or ``None`` if the peripheral never answered.

        Raises:
            DeviceCommandError: If ``bleak`` is missing (with install guidance).
            DeviceAuthenticationError: If the companion needs a pairing PIN we don't have or
                that was rejected.
        """
        try:
            return await self._create_ble_with_retry(mesh_core)
        except ImportError as exc:
            raise DeviceCommandError(
                "Bluetooth support requires the 'bleak' package, which isn't installed. "
                "Run `pip install -e .` (or `pip install bleak`), use a USB device, or "
                "run with --mock."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - translate auth failures; re-raise the rest
            # A PIN-protected companion accepts the link-layer connection but rejects the
            # GATT subscribe with an authentication error ("Insufficient Authentication" /
            # "Insufficient Encryption" / "not paired"). That's not a dropped link — it's a
            # missing bond — so surface a clean, actionable message instead of a raw traceback
            # (which is what a bare BleakGATTProtocolError would produce). Anything else
            # propagates unchanged so genuine link-loss still flows to is_connection_lost.
            if not _is_ble_auth_error(exc):
                raise
            # On Windows this can also happen with the *right* PIN when a stale, unauthenticated
            # "Just Works" bond from an older attempt is in the way: is_paired is true so the
            # ProvidePin step above was skipped, yet the bond can't unlock the characteristic.
            # Clear it, pair with the PIN, and retry the connect exactly once before giving up.
            if allow_repair and await self._pair_ble_windows(force=True):
                return await self._open_ble(mesh_core, allow_repair=False)
            # On macOS the same rejection means the opposite thing: it is not the end of a
            # pairing attempt but the *start* of one, because touching the authenticated
            # characteristic is the only way to make CoreBluetooth pair at all. The OS is
            # putting its Passkey dialog up as we unwind. Wait for the person to answer it.
            if allow_repair and sys.platform == "darwin":
                return await self._open_ble_after_macos_pairing(mesh_core, exc)
            raise await self._ble_auth_failure() from exc

    async def _open_ble_after_macos_pairing(self, mesh_core, cause: BaseException):  # type: ignore[no-untyped-def]
        """Re-open the link while macOS is running the Passkey dialog it just raised.

        CoreBluetooth exposes no pairing API — Apple's model is that a peripheral pairs
        *implicitly* when something touches a characteristic that requires encryption. The
        companion firmware puts its UART characteristic at ENC+MITM precisely so that
        happens (``SECMODE_ENC_WITH_MITM`` on nRF52, ``ESP_GATT_PERM_*_ENC_MITM`` on
        ESP32), so the subscribe fails with "Insufficient Authentication" *and* that
        failure is what makes macOS ask for the code. The GATT operation is already dead by
        then; the bond it started arrives seconds later, once a human has typed six digits.

        Failing there reported an error for a pairing that was in fact succeeding, and the
        device connected on the next attempt — the first having silently done the work. So
        retry rather than give up, for long enough to cover the typing.

        Args:
            mesh_core: The imported ``meshcore.MeshCore`` class.
            cause: The authentication failure that opened the dialog, chained onto the
                final error if the pairing never completes.

        Returns:
            The connected ``MeshCore`` client, or ``None`` if a later attempt opened the
            transport but the peripheral never answered the identity handshake.

        Raises:
            DeviceAuthenticationError: If every attempt was still refused — the dialog was
                dismissed, or the code entered was wrong.
        """
        for attempt in range(_BLE_MACOS_PAIRING_ATTEMPTS):
            await asyncio.sleep(_BLE_MACOS_PAIRING_DELAY_S)
            try:
                return await self._open_ble(mesh_core, allow_repair=False)
            except DeviceAuthenticationError:
                _log.debug(
                    "BLE bond with %s not established yet (attempt %d/%d)",
                    self._address,
                    attempt + 1,
                    _BLE_MACOS_PAIRING_ATTEMPTS,
                )
        raise await self._ble_auth_failure() from cause

    async def _create_ble_with_retry(self, mesh_core):  # type: ignore[no-untyped-def]
        """Open the owned BLE client, retrying the transport-level failures that are transient.

        The meshcore client raises a bare ``ConnectionError`` when the *link itself* could
        not be opened — the peripheral wasn't found during bleak's internal lookup, or the
        link-layer connect timed out. On Windows both are routinely transient: a companion
        advertising on a slow interval is easily missed by a single scan window, and a
        connect attempted right after the discovery scan can race the radio. Users learned
        to work around it by re-selecting the device, which is nothing but a manual retry —
        so retry here, briefly, before surfacing the failure. The already-discovered
        ``BLEDevice`` (when the picker's scan produced one) is passed through so the client
        connects to it directly instead of re-discovering the address.

        Only ``ConnectionError`` is retried: a PIN/bond rejection or any other GATT failure
        propagates unchanged on the first attempt so the auth handling in :meth:`_open_ble`
        (and a genuine wrong-PIN) is never looped. Each attempt builds its own client through
        :meth:`_connect_owned_ble`, which closes it before letting any failure out — a
        retried attempt therefore starts from a released link, never a leaked one.

        Args:
            mesh_core: The imported ``meshcore.MeshCore`` class.

        Returns:
            The connected ``MeshCore`` client, or ``None`` if the transport connected but
            the peripheral never answered the identity handshake.

        Raises:
            DeviceCommandError: If every attempt failed to open the link — said as such,
                because this is the one failure where the device was never reached at all,
                and "didn't answer as a MeshCore device" sent people looking at the firmware.
        """
        last_exc: ConnectionError | None = None
        for attempt in range(_BLE_CONNECT_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_BLE_CONNECT_RETRY_DELAY_S)
            try:
                return await self._connect_owned_ble(mesh_core)
            except ConnectionError as exc:
                _log.debug(
                    "BLE link to %s failed to open (attempt %d/%d): %s",
                    self._address,
                    attempt + 1,
                    _BLE_CONNECT_ATTEMPTS,
                    exc,
                )
                last_exc = exc
        assert last_exc is not None  # the loop always runs; only ConnectionError falls through
        where = self._address or "the selected Bluetooth device"
        _log.warning("BLE link to %s never opened: %s", where, last_exc)
        raise DeviceCommandError(
            f"couldn't open a Bluetooth link to {where} ({_BLE_CONNECT_ATTEMPTS} tries) — it "
            "wasn't found, or didn't accept the connection. It may be out of range, powered "
            "off, or connected to a phone or another computer (a companion takes one "
            "connection at a time)."
        ) from last_exc

    async def _connect_owned_ble(self, mesh_core):  # type: ignore[no-untyped-def]
        """Build the meshcore BLE client here and connect it, so we own its teardown.

        ``MeshCore.create_ble`` assembles a client, calls ``connect()`` on it, and hands it
        back *only on success* — so a connect that **raises** leaves that client, and the
        bleak link it has already opened, orphaned inside the library with no reference we
        could close. That is not a hypothetical: a PIN-protected companion answers the
        unbonded notify-subscribe with a GATT authentication error, which is raised from
        deep inside ``connect()`` after bleak has brought the link up. Our own
        :meth:`disconnect` then does nothing (``_mc`` was never assigned), Windows holds the
        ACL link for the life of the process, and the peripheral — still believing it has a
        peer — **stops advertising**. The PIN dialog that opens next therefore asks for a
        code it can no longer deliver: the retry can't find the device, and the user reads a
        correct PIN being refused. Assembling the same two objects here costs three lines and
        keeps the handle, so every exit puts the link back down.

        Both public failure shapes are covered, matching what ``create_ble`` does on the one
        it bothers to handle: a raise (closed, then re-raised) and a ``None`` from the
        identity handshake (closed, then reported as "not a companion").

        Args:
            mesh_core: The imported ``meshcore.MeshCore`` class.

        Returns:
            The connected ``MeshCore`` client, or ``None`` if the transport opened but the
            peripheral never answered the identity handshake.

        Raises:
            Exception: Whatever ``connect`` raised — but not before the link is closed.
        """
        from meshcore import BLEConnection

        # Withhold the PIN on macOS. Handed one, ``BLEConnection.connect`` calls bleak's
        # ``client.pair()``, and CoreBluetooth has no pairing API at all — the macOS
        # backend raises ``NotImplementedError`` outright — whereupon the library
        # disconnects and re-raises, so supplying a *correct* PIN is what breaks the
        # connection. Apple's model is that pairing is the OS's to run, not ours: the
        # companion firmware puts its UART characteristic at ENC+MITM
        # (``SECMODE_ENC_WITH_MITM`` on nRF52, ``ESP_GATT_PERM_*_ENC_MITM`` on ESP32), so
        # the unbonded subscribe below is answered with "Insufficient Authentication",
        # and macOS reacts by running Passkey Entry and prompting for the code itself.
        # Saying nothing here is therefore what *lets* a PIN-protected companion bond;
        # the OS keeps the bond, and later connections need no PIN. Windows and Linux do
        # expose explicit pairing, and keep it (see :meth:`_pair_ble_windows`).
        pin = None if sys.platform == "darwin" else self._pin
        connection = BLEConnection(address=self._address, device=self._ble_device, pin=pin)
        mc = mesh_core(
            connection,
            default_timeout=self._connect_timeout,
            auto_reconnect=False,
        )
        try:
            started = await mc.connect()
        except BaseException:
            self._ble_client = getattr(connection, "client", None)
            await MeshCoreDevice._discard_meshcore(mc)
            await self._release_ble_client()
            raise
        # Take our own reference to the bleak client now, while ``BLEConnection`` still holds
        # one. It drops it the instant the peripheral goes away, and by then nothing else can
        # reach the object that needs closing (see :meth:`_release_ble_client`).
        self._ble_client = getattr(connection, "client", None)
        if started is None:
            await MeshCoreDevice._discard_meshcore(mc)
            await self._release_ble_client()
            return None
        return mc

    @staticmethod
    async def _discard_meshcore(mc) -> None:  # type: ignore[no-untyped-def]
        """Close a client whose ``connect`` didn't complete. Bounded, and never raises.

        The graceful ``mc.disconnect()`` is **not sufficient on its own here**, and that is
        the whole subtlety of this path. ``ConnectionManager.disconnect`` closes the
        transport only ``if self._is_connected`` — a flag it sets *after*
        ``connection.connect()`` returns. A connect that raised (the GATT authentication
        error, thrown from ``start_notify`` well after bleak brought the link up) never got
        that far, so the manager is certain there is nothing to close while ``BLEConnection``
        is still holding a live, connected ``BleakClient``. Calling only the graceful path
        therefore looks like a teardown and leaks the link anyway — which is exactly how the
        first attempt at this fix still left the peripheral off the air.

        So both halves run: the graceful call first (it stops the dispatcher and cancels any
        reconnect task), then the transport closed directly, which is what actually drops the
        link. ``BLEConnection.disconnect`` re-checks ``client.is_connected``, so the second
        close is a no-op whenever the first one did the job.

        Shielded on purpose. The other way into this method is a probe whose ``wait_for``
        expired and cancelled the handshake mid-flight; a plain ``await`` would then be
        cancelled itself the moment it suspended, abandoning the very teardown it was called
        to perform and leaking exactly the link this exists to close. Shielding lets the
        close finish on its own while the cancellation continues to propagate to our caller.

        Args:
            mc: A half-open ``meshcore.MeshCore`` client.
        """

        async def _close() -> None:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=_DISCARD_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 - the forced close is the real one
                _log.debug("graceful discard failed (%s); forcing the transport close", exc)
            await MeshCoreDevice._force_close_transport(mc)

        closing = asyncio.ensure_future(_close())
        try:
            await asyncio.shield(closing)
        except BaseException as exc:  # noqa: BLE001 - teardown of a doomed client
            # Includes CancelledError: swallowed here only so the caller's own exception
            # (or cancellation) is the one that propagates. ``closing`` runs on regardless.
            _log.debug("discarding a half-open BLE client: %s", exc)

    @staticmethod
    async def _force_close_transport(mc) -> None:  # type: ignore[no-untyped-def]
        """Cancel the dispatcher and close the raw transport directly. Best-effort, silent.

        The escape hatch from both library traps: a dispatcher stop that deadlocks on its own
        ``queue.join()``, and a connection manager that refuses to close a transport it never
        recorded as connected. Reaching past both is what actually releases the serial port or
        the BLE link.

        Args:
            mc: The ``meshcore.MeshCore`` client to tear down.
        """
        try:
            stop = getattr(mc, "stop", None)
            if stop is not None:
                stop()
        except Exception as exc:  # noqa: BLE001 - best-effort force-stop
            _log.debug("dispatcher force-stop failed: %s", exc)
        try:
            raw = getattr(getattr(mc, "connection_manager", None), "connection", None)
            if raw is not None:
                await asyncio.wait_for(raw.disconnect(), timeout=_FORCE_DISCONNECT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - the link may already be gone
            _log.debug("forced transport close failed: %s", exc)

    async def _pair_ble_windows(self, *, force: bool) -> bool:
        """Establish an authenticated BLE bond via the WinRT ProvidePin ceremony (Windows only).

        This is the one place a Bluetooth passkey is actually delivered to the peripheral.
        bleak's own ``pair()`` on Windows is hardcoded to the ``CONFIRM_ONLY`` ("Just Works")
        ceremony and never sends a PIN, so a companion that demands passkey pairing can't be
        bonded through bleak at all — its authenticated UART characteristic keeps rejecting the
        notify subscribe with *Insufficient Authentication*. We instead run the ``PROVIDE_PIN``
        ceremony directly against WinRT (the same one the Windows "Add device" dialog uses),
        handing it :attr:`_pin`, which yields the ``ENCRYPTION_AND_AUTHENTICATION`` bond the
        characteristic needs. Windows persists the bond, so subsequent sessions reconnect with
        no PIN required.

        Best-effort and self-contained: it returns a bool rather than raising, and swallows any
        error (winrt projection absent, device out of range, API quirk) so the caller simply
        falls through to the normal connect — whose auth-error translation still yields the
        right message. A no-op (returns ``False``) off Windows, when no PIN is set, or when the
        address isn't a parseable MAC.

        Args:
            force: When ``False``, an existing bond is trusted and reused (the fast path). When
                ``True``, any existing bond is torn down first and re-created with the PIN — used
                to heal a stale, unauthenticated "Just Works" bond that a plain reconnect can't.

        Returns:
            ``True`` if an authenticated bond exists afterward (freshly paired or already
            bonded), ``False`` otherwise.
        """
        if sys.platform != "win32" or not self._pin:
            return False
        address = self._ble_address_int(self._address or "")
        if address is None:
            return False
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEDevice
            from winrt.windows.devices.enumeration import (
                DevicePairingKinds,
                DevicePairingProtectionLevel,
                DevicePairingResultStatus,
            )
        except Exception as exc:  # noqa: BLE001 - winrt projection unavailable; fall through
            _log.debug("BLE PIN pairing unavailable (winrt import failed): %s", exc)
            return False

        # Every exit records what it found in ``_ble_pairing``, so a refusal further on can
        # say which of these it was — the bool alone is only enough to decide on a retry.
        device = None
        try:
            device = await BluetoothLEDevice.from_bluetooth_address_async(address)
            if device is None:
                self._ble_pairing = _BlePairing("absent")
                return False  # out of range / not connectable right now
            pairing = device.device_information.pairing
            if pairing.is_paired:
                if not force:
                    self._ble_pairing = _BlePairing("reused")
                    return True  # trust the existing (authenticated) bond — fast path
                # Tear the stale bond down, then re-fetch: the pairing object is a snapshot and
                # won't reflect the unpair, so a fresh device_information is needed to re-pair.
                await pairing.unpair_async()
                MeshCoreDevice._close_ble_device(device)  # release the pre-unpair handle
                device = await BluetoothLEDevice.from_bluetooth_address_async(address)
                if device is None:
                    self._ble_pairing = _BlePairing("absent")
                    return False
                pairing = device.device_information.pairing
            custom = pairing.custom
            pin = self._pin

            def _provide_pin(_sender, args) -> None:  # noqa: ANN001 - winrt callback
                # The peripheral asked for a passkey; hand it the one we were given.
                args.accept_with_pin(pin)

            token = custom.add_pairing_requested(_provide_pin)
            try:
                result = await custom.pair_with_protection_level_async(
                    DevicePairingKinds.PROVIDE_PIN,
                    DevicePairingProtectionLevel.ENCRYPTION_AND_AUTHENTICATION,
                )
            finally:
                custom.remove_pairing_requested(token)
            status = int(result.status)
            ok = status in (
                int(DevicePairingResultStatus.PAIRED),
                int(DevicePairingResultStatus.ALREADY_PAIRED),
            )
            name = str(getattr(result.status, "name", status)).lower().replace("_", " ")
            if ok:
                self._ble_pairing = _BlePairing("paired")
                _log.debug("BLE ProvidePin pairing for %s: %s", self._address, name)
            else:
                # A warning, not debug: this is the line that tells a wrong PIN from a device
                # that refused to pair, and the default log level would otherwise hide it.
                self._ble_pairing = _BlePairing("failed", name)
                _log.warning("BLE PIN pairing with %s failed: %s", self._address, name)
            return ok
        except Exception as exc:  # noqa: BLE001 - best-effort; caller falls through on False
            self._ble_pairing = _BlePairing("error", str(exc))
            _log.warning("BLE PIN pairing with %s raised: %s", self._address, exc)
            return False
        finally:
            MeshCoreDevice._close_ble_device(device)

    @staticmethod
    def _ble_address_int(address: str) -> int | None:
        """Parse a ``AA:BB:CC:DD:EE:FF`` (or dash-separated) MAC into the ulong WinRT wants.

        Args:
            address: The Bluetooth address string bleak reported for the device.

        Returns:
            The 48-bit address as an int, or ``None`` if it isn't a 12-hex-digit MAC (e.g. a
            CoreBluetooth UUID on macOS, where this pairing path doesn't apply anyway).
        """
        cleaned = address.replace(":", "").replace("-", "").strip()
        if len(cleaned) != 12:
            return None
        try:
            return int(cleaned, 16)
        except ValueError:
            return None

    @staticmethod
    def _close_ble_device(device) -> None:  # noqa: ANN001 - winrt BluetoothLEDevice
        """Release a WinRT ``BluetoothLEDevice`` handle, dropping the OS's link to the peripheral.

        Every ``BluetoothLEDevice.from_bluetooth_address_async`` hands back an ``IClosable`` that
        pins the operating system's ACL connection to the radio open for as long as the object is
        alive. Unpairing removes the *bond* but never tears down that *link* — the link only goes
        away when the last handle to it closes. If we leak the handle, Windows reports the device
        as still "connected" (just unpaired), the peripheral never sees a clean disconnect, and it
        refuses to re-pair until it is power-cycled. So every helper that opens one of these must
        close it, even on the error paths.

        Best-effort and silent: a missing ``close`` projection or a double-close is not worth
        surfacing during teardown.
        """
        try:
            if device is not None:
                device.close()
        except Exception as exc:  # noqa: BLE001 - releasing a handle must never raise
            _log.debug("BLE device handle close failed: %s", exc)

    @staticmethod
    async def is_ble_paired(address: str) -> bool:
        """Whether Windows currently holds a bond for the BLE peripheral at ``address``.

        The read-only companion to :meth:`_pair_ble_windows` / :meth:`unpair_ble`: it asks
        WinRT whether an OS-level pairing exists, so the UI can decide whether an "unpair"
        affordance is meaningful (a device bonded with a PIN) or moot (an open companion that
        never bonded, or a serial link). It reflects the *OS bond*, not MeshTerm's remembered
        record — the two are independent — and holds true across sessions even when this run
        supplied no PIN, because Windows persists the bond.

        Best-effort and self-contained: returns ``False`` (rather than raising) off Windows,
        when the winrt projection is unavailable, when the address isn't a parseable MAC, or on
        any WinRT hiccup — so a caller can treat it as a plain "is there anything to unpair?".

        Args:
            address: The Bluetooth MAC (``AA:BB:CC:DD:EE:FF`` or dash-separated) to query.

        Returns:
            ``True`` only when Windows reports a live bond for the device.
        """
        if sys.platform != "win32":
            return False
        addr = MeshCoreDevice._ble_address_int(address or "")
        if addr is None:
            return False
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEDevice
        except Exception as exc:  # noqa: BLE001 - winrt projection unavailable; nothing to unpair
            _log.debug("BLE pairing query unavailable (winrt import failed): %s", exc)
            return False
        device = None
        try:
            device = await BluetoothLEDevice.from_bluetooth_address_async(addr)
            if device is None:
                return False
            return bool(device.device_information.pairing.is_paired)
        except Exception as exc:  # noqa: BLE001 - a status hiccup is not a bond
            _log.debug("BLE pairing query failed for %s: %s", address, exc)
            return False
        finally:
            MeshCoreDevice._close_ble_device(device)

    @staticmethod
    async def unpair_ble(address: str) -> bool:
        """Drop the Windows OS-level bond for the BLE peripheral at ``address`` (Windows only).

        The inverse of :meth:`_pair_ble_windows`: it removes the persisted
        ``ENCRYPTION_AND_AUTHENTICATION`` bond so the next connection has to re-run the PIN
        ceremony from scratch — the "forget this pairing" primitive behind the quit dialog's
        *Unpair & quit*. It touches only the OS bond, never MeshTerm's remembered-device record,
        which is deliberately left intact (the device keeps its friendly name and stays in the
        picker; it just asks for its PIN again next time).

        Call it only *after* the companion link is torn down — you can't cleanly drop a bond that
        an open connection is still using. Best-effort and self-contained: returns a bool rather
        than raising, and no-ops (returns ``False``) off Windows, when winrt is unavailable, when
        the address isn't a MAC, or when there is no bond to remove.

        Args:
            address: The Bluetooth MAC (``AA:BB:CC:DD:EE:FF`` or dash-separated) to unpair.

        Returns:
            ``True`` if a bond was removed, ``False`` if there was nothing to unpair or the
            attempt failed.
        """
        if sys.platform != "win32":
            return False
        addr = MeshCoreDevice._ble_address_int(address or "")
        if addr is None:
            return False
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEDevice
            from winrt.windows.devices.enumeration import DeviceUnpairingResultStatus
        except Exception as exc:  # noqa: BLE001 - winrt projection unavailable; fall through
            _log.debug("BLE unpair unavailable (winrt import failed): %s", exc)
            return False
        device = None
        try:
            device = await BluetoothLEDevice.from_bluetooth_address_async(addr)
            if device is None:
                return False
            pairing = device.device_information.pairing
            if not pairing.is_paired:
                return False  # nothing bonded — treat as a no-op success-of-intent
            result = await pairing.unpair_async()
            status = int(result.status)
            ok = status == int(DeviceUnpairingResultStatus.UNPAIRED)
            _log.debug("BLE unpair for %s: status=%d ok=%s", address, status, ok)
            return ok
        except Exception as exc:  # noqa: BLE001 - best-effort teardown; never crash exit
            _log.debug("BLE unpair attempt failed for %s: %s", address, exc)
            return False
        finally:
            # Closing the handle is what actually drops the OS's link to the peripheral; without
            # it the device stays "connected" after the unpair and won't re-pair until rebooted.
            MeshCoreDevice._close_ble_device(device)

    async def _ble_os_bonded(self) -> bool:
        """Whether Windows holds a bond for this device — asked only to explain a refusal.

        An instance hook over :meth:`is_ble_paired` so tests can answer it without the OS.
        """
        return await MeshCoreDevice.is_ble_paired(self._address or "")

    async def _ble_auth_failure(self) -> DeviceAuthenticationError:
        """Name *which* authentication failure this was, and what fixes it.

        The GATT refusal looks the same on the wire whatever caused it, but the causes don't
        share a remedy, and one sentence covering all of them sent people to the wrong one — a
        stale Windows bond, say, was reported as "requires a PIN" to someone whose device
        Windows plainly showed as paired. So the error is built from what is actually known:

        * macOS — the OS runs the pairing in its own dialog; say so.
        * No PIN, and Windows holds a bond — the bond is stale (the device was reflashed,
          reset, or given a new PIN since), and re-pairing is the fix.
        * No PIN, no bond — the device needs its PIN.
        * A PIN, and the Windows pairing ceremony reported a status — a wrong PIN, a device
          that refused to pair, or some other status, each named.
        * A PIN, the pairing succeeded, and the device still refused — it is holding an old
          bond for this computer.
        * A PIN and nothing more known (Linux, or no WinRT) — the PIN was rejected.

        Every case is also logged, so a log sent in from another machine says which it was.

        Returns:
            The error to raise, carrying a one-line :attr:`~DeviceAuthenticationError.hint`
            for the PIN dialog.
        """
        where = self._address or "the selected Bluetooth device"
        # Windows' bond changes the message in exactly one case, so it is asked only there.
        bonded = sys.platform == "win32" and not self._pin and await self._ble_os_bonded()
        message, hint = self._ble_auth_diagnosis(where, bonded)
        _log.warning("BLE connect to %s refused: %s", where, message)
        return DeviceAuthenticationError(message, hint=hint)

    def _ble_auth_diagnosis(self, where: str, os_bonded: bool) -> tuple[str, str]:
        """The message and dialog hint for :meth:`_ble_auth_failure` (see there for the cases).

        Args:
            where: The device, as the message names it.
            os_bonded: Whether Windows holds a bond (asked only when no PIN was given).

        Returns:
            ``(message, hint)``.
        """
        if sys.platform == "darwin":
            # macOS collects the code itself, in its own dialog, so --ble-pin is not the
            # remedy here and naming it would send the reader somewhere that cannot help.
            return (
                f"{where} was not paired. macOS asks for the pairing code in its own dialog "
                "rather than through MeshTerm — enter the 6-digit code shown on the device "
                "(or in the MeshCore app) when it appears, and the bond is remembered for "
                "next time. If no dialog appeared, check that Bluetooth is allowed for this "
                "terminal in System Settings > Privacy & Security > Bluetooth.",
                "",
            )
        if not self._pin:
            if os_bonded:
                return (
                    f"Windows has {where} paired, but the device refused that pairing — it is "
                    "probably out of date (the device was reflashed, reset, or given a new PIN "
                    "since). Pass --ble-pin <PIN> and MeshTerm will pair it again, or remove it "
                    "in Settings > Bluetooth and reconnect.",
                    "Windows' saved pairing was refused — the PIN pairs it again.",
                )
            return (
                f"{where} requires a Bluetooth pairing PIN. Pass it with --ble-pin <PIN> (the "
                "6-digit code shown on the device or in the MeshCore app). On Windows you may "
                "also need to pair the device once in Settings > Bluetooth before it will "
                "connect.",
                "",
            )
        pairing = self._ble_pairing
        outcome = pairing.outcome if pairing else ""
        status = pairing.status if pairing else ""
        if outcome == "failed" and status in _PAIRING_REFUSED:
            return (
                f"{where} refused to pair ({status}) — it may be connected to a phone or "
                "another computer, or still holding an old pairing for this one. Disconnect "
                "it there or restart it, then try again.",
                "The device refused to pair — restart it and try again.",
            )
        if outcome == "failed" and status not in _PAIRING_WRONG_PIN:
            return (
                f"Windows couldn't pair with {where} ({status}). Remove it in Settings > "
                "Bluetooth, restart the device, and try again.",
                f"Windows couldn't pair ({status}).",
            )
        if outcome == "absent":
            return (
                f"Windows couldn't reach {where} to pair it — it may be out of range, asleep, "
                "or connected to another device.",
                "Windows couldn't reach the device to pair it.",
            )
        if outcome == "error":
            return (
                f"Windows' pairing call failed for {where}: {status}",
                "Windows' pairing call failed — the log has the detail.",
            )
        if outcome in ("paired", "reused"):
            return (
                f"Windows paired with {where}, but the device still refuses the connection — "
                "it is probably holding an old pairing for this computer. Restart the device, "
                "remove it in Settings > Bluetooth, and try again.",
                "Paired, but still refused — restart the device and retry.",
            )
        return (
            f"{where} rejected the Bluetooth PIN — it needs pairing and the PIN provided "
            "wasn't accepted. Double-check the 6-digit code shown on the device (or in the "
            "MeshCore app) and pass it with --ble-pin, then try again.",
            "That PIN was rejected — check the code and try again.",
        )

    def _no_response_message(self) -> str:
        """A clean, recoverable error for an endpoint that didn't answer as a companion."""
        if self._transport == "ble":
            # The link opened (a link that didn't has its own message), so range and power
            # are not the question here — what answered is.
            where = self._address or "the selected Bluetooth device"
            return (
                f"connected to {where} over Bluetooth, but it never answered as a MeshCore "
                "companion — it may be running repeater or room server firmware, or be busy "
                "with another app."
            )
        if self._transport == "tcp":
            where = self.endpoint or "the selected network device"
            return (
                f"no response from a MeshCore companion at {where}; check the host and port, "
                "and that the device is powered on, reachable on the network, and not already "
                "connected to another client."
            )
        return (
            f"no response from a MeshCore companion on {self._port}; it may not be a "
            "MeshCore device, or it may be powered off or in use by another program."
        )

    async def link_present(self) -> bool:  # noqa: D102 - inherited docstring
        if self._mc is None:
            return True  # not connected yet / already torn down — nothing to declare lost
        if self._transport in ("ble", "tcp"):
            # The meshcore client flips ``is_connected`` to False the moment the transport
            # drops — bleak's disconnect callback for BLE, a broken socket for TCP — so this is
            # the network/Bluetooth analogue of the serial port-enumeration check: a cheap,
            # non-transmitting liveness read.
            try:
                return bool(self._mc.is_connected)
            except Exception:  # noqa: BLE001 - a status hiccup must not fake a disconnect
                return True
        if not self._port:
            return True  # no port recorded (shouldn't happen once connected) — can't tell
        return serial_port_present(self._port)

    async def disconnect(self) -> None:
        """Close the connection and release resources. Idempotent, and bounded in time.

        The graceful ``meshcore`` teardown is given :data:`_DISCONNECT_TIMEOUT_S` to finish.
        That bound matters: the library's dispatcher stop awaits ``queue.join()``, but its
        processor task exits after handling at most one event once stopped — so with two or
        more events queued at that instant (a routine advert/RX-log burst on a live mesh)
        the join deadlocks and a quit would hang until the exit watchdog force-kills the
        process. When the graceful path doesn't return in time it is cancelled and the
        teardown is forced instead: the dispatcher task is cancelled synchronously and the
        raw transport is closed directly (also bounded), so the port/link is still released.

        Over Bluetooth the ``bleak`` client is then closed directly as well, because none of
        the above reaches it once the peripheral is the thing that went away — see
        :meth:`_release_ble_client`.
        """
        if self._mc is None:
            await self._release_ble_client()  # a link that dropped before we ever tore it down
            return
        mc, self._mc = self._mc, None
        disconnect = getattr(mc, "disconnect", None)
        if disconnect is None:
            await self._release_ble_client()
            return
        try:
            await asyncio.wait_for(disconnect(), timeout=_DISCONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            _log.debug("graceful disconnect timed out; forcing transport teardown")
            # Forced teardown: cancel the (possibly wedged) dispatcher task without awaiting
            # the deadlocked join, then close the underlying transport so the serial port /
            # BLE link is actually released. Best-effort — nothing here may block the exit.
            await MeshCoreDevice._force_close_transport(mc)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise; fall to forced path
            _log.debug("graceful disconnect failed (%s); forcing transport teardown", exc)
            await MeshCoreDevice._force_close_transport(mc)
        await self._release_ble_client()

    async def _release_ble_client(self) -> None:
        """Close the ``bleak`` client itself, whatever the library believes about its state.

        The one teardown step that cannot be delegated, and the reason a Bluetooth session
        could not be rebuilt after the companion was switched off. Every layer above declines
        to act once the *peripheral* is what went away, each for its own locally-sensible
        reason:

        * ``BLEConnection.handle_disconnect`` — bleak's own dropped-link callback — restores
          the connection's fields to what the caller originally passed, which sets
          ``self.client`` back to ``None``. The live client object is simply let go of.
        * ``BLEConnection.disconnect`` then guards on ``self.client and
          self.client.is_connected``, so it has nothing to close and does nothing.
        * ``ConnectionManager.disconnect`` guards on ``self._is_connected``, which its own
          drop handler already cleared, so it does nothing either.
        * :meth:`_force_close_transport` reaches past both — but only as far as that same
          ``BLEConnection``, whose ``disconnect`` is the no-op above.

        So on a drop nobody ever calls ``BleakClient.disconnect()``, and on Windows the WinRT
        ``BluetoothLEDevice`` and its GATT session stay open for the life of the process. The
        next connect to that address is then served a broken service table: the link comes up,
        and subscribing to the UART characteristic fails with *"Characteristic
        6E400003-… was not found!"* — a device that is advertising, healthy, and unreachable
        for as long as the app keeps running. A fresh process connects to it perfectly.

        Hence our own reference, taken at connect (see :meth:`_connect_owned_ble`) while the
        library still has one to give, and closed here **unconditionally** — never guarded on
        ``is_connected``, since the case that matters is precisely the one where it is already
        ``False``. Bounded and silent: the link is already gone, and a teardown may not raise
        or hang. Idempotent, and a no-op on serial and TCP, which have no client to hold.
        """
        client, self._ble_client = self._ble_client, None
        if client is None:
            return
        disconnect = getattr(client, "disconnect", None)
        if disconnect is None:  # pragma: no cover - every bleak client has one
            return
        try:
            await asyncio.wait_for(disconnect(), timeout=_FORCE_DISCONNECT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - the link is already gone; best-effort
            _log.debug("releasing the bleak client failed: %s", exc)

    def _require(self):  # type: ignore[no-untyped-def]
        """Return the live client or raise if not connected."""
        if self._mc is None:
            raise RuntimeError("Device is not connected; call connect() first.")
        return self._mc

    async def get_self_info(self) -> dict:  # noqa: D102 - inherited docstring
        mc = self._require()
        result = await mc.commands.send_appstart()
        info = dict(getattr(result, "payload", {}) or {})
        # The firmware sends TX power as a signed byte (it accepts down to -9 dBm) and the
        # library reads it unsigned, so a negative power would arrive as 247 and up.
        power = info.get("tx_power")
        if isinstance(power, int) and power > 127:
            info["tx_power"] = power - 256
        return info

    async def get_device_info(self) -> dict:  # noqa: D102 - inherited docstring
        mc = self._require()
        try:
            result = await mc.commands.send_device_query()
        except Exception:  # noqa: BLE001 - older firmware lacks the query; unknown, not fatal
            return {}
        return dict(getattr(result, "payload", {}) or {})

    #: How long the contacts stream may go quiet before a read is called failed (seconds).
    #: The library's own ``get_contacts`` arms one future for the *whole* dump and never
    #: re-arms it, so a table that takes longer than its five seconds to stream fails with
    #: "no event received" however healthy the radio is — which is what a node holding four
    #: hundred contacts does over Bluetooth. What actually means "the companion stopped
    #: answering" is a gap *between* records, so that is what this times: the dump may take
    #: as long as it takes, for as long as it keeps arriving.
    _CONTACTS_IDLE_S = 6.0

    async def _contacts_payload(
        self, mc, *, retries: int = 3, delay: float = 0.5, idle: float | None = None
    ) -> dict:  # noqa: ANN001
        """Fetch the raw contacts map, retrying a read that stalls or is refused.

        The companion intermittently refuses or drops a contacts request — a recoverable
        timing hiccup — so the read is attempted a few times with a short backoff before a
        clean, actionable error is raised. One attempt is :meth:`_stream_contacts`.

        Args:
            mc: The connected ``MeshCore`` client.
            retries: Number of extra attempts after the first.
            delay: Seconds to wait between attempts.
            idle: Seconds of silence that end an attempt (default :data:`_CONTACTS_IDLE_S`).

        Returns:
            The contacts payload mapping (possibly empty).

        Raises:
            DeviceCommandError: If every attempt fails to retrieve contacts.
        """
        gap = self._CONTACTS_IDLE_S if idle is None else idle
        reason = ""
        for attempt in range(retries + 1):
            payload, reason = await self._stream_contacts(mc, gap)
            if payload is not None:
                return payload
            if attempt < retries:
                await asyncio.sleep(delay)
        raise DeviceCommandError(
            f"could not read contacts from the radio ({reason}). "
            "The companion didn't respond in time — this is usually transient; "
            "retry, or power-cycle/reconnect the radio if it persists."
        )

    async def _stream_contacts(self, mc, idle: float) -> tuple[dict | None, str]:  # noqa: ANN001
        """Run one contacts read, ending it on *silence* rather than on a deadline.

        The contacts table arrives as one ``NEXT_CONTACT`` frame per record and a closing
        ``CONTACTS`` frame holding the whole map. A dump is therefore not one answer that is
        either late or on time, it is a stream, and the only thing that distinguishes a slow
        big table from a radio that has stopped talking is how long it has been since the
        last record. So each record restarts the clock and only ``idle`` seconds of quiet
        end the attempt — which is the whole of the fix for a four-hundred-contact node,
        whose dump simply takes longer than any fixed deadline the library would allow it.

        Args:
            mc: The connected ``MeshCore`` client.
            idle: Seconds of silence that end the attempt.

        Returns:
            ``(payload, "")`` on success, or ``(None, reason)`` describing how it ended.
        """
        from meshcore import EventType

        loop = asyncio.get_running_loop()
        finished: asyncio.Future = loop.create_future()
        arrived = asyncio.Event()
        seen = False
        refusal = ""

        def on_record(event) -> None:  # noqa: ANN001 - meshcore Event
            nonlocal seen
            seen = True
            arrived.set()

        def on_end(event) -> None:  # noqa: ANN001 - meshcore Event
            if not finished.done():
                finished.set_result(event)

        def on_error(event) -> None:  # noqa: ANN001 - meshcore Event
            # An ERROR frame carries no request id, so it is only ours while nothing else
            # can have earned it: before the first record it is this request being refused
            # (a companion too busy to serve a dump answers ERR_CODE_BAD_STATE), and after
            # one it belongs to whatever else is on the link — a battery poll, a courier
            # send — where treating it as ours is how a read that was working stopped.
            nonlocal refusal
            payload = getattr(event, "payload", {}) or {}
            refusal = str(payload.get("reason", payload))
            if not seen and not finished.done():
                finished.set_result(None)

        subscriptions = [
            mc.subscribe(EventType.NEXT_CONTACT, on_record),
            mc.subscribe(EventType.CONTACTS, on_end),
            mc.subscribe(EventType.ERROR, on_error),
        ]
        try:
            await mc.commands.get_contacts_async()
            while not finished.done():
                arrived.clear()
                ticking = asyncio.ensure_future(arrived.wait())
                try:
                    await asyncio.wait(
                        {finished, ticking},
                        timeout=idle,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    ticking.cancel()
                if not finished.done() and not arrived.is_set():
                    break
        finally:
            for subscription in subscriptions:
                subscription.unsubscribe()
        event = finished.result() if finished.done() else None
        if event is not None:
            return dict(getattr(event, "payload", {}) or {}), ""
        if refusal and not seen:
            return None, refusal
        if seen:
            return None, "no event received during contacts retrieval — it stopped partway"
        return None, "no event received during contacts retrieval"

    async def get_contacts(self) -> list[Contact]:  # noqa: D102 - inherited docstring
        mc = self._require()
        payload = await self._contacts_payload(mc)
        contacts: list[Contact] = []
        for name, info in payload.items():
            info = info or {}
            lat, lon = _contact_location(info)
            contacts.append(
                Contact(
                    name=info.get("adv_name", name),
                    public_key=info.get("public_key", ""),
                    key_prefix=info.get("public_key", "")[:12],
                    last_seen=advert_time(info.get("last_advert")),
                    node_type=_as_int(info.get("type", info.get("adv_type"))),
                    lat=lat,
                    lon=lon,
                    route_hops=_contact_route(info),
                )
            )
        return contacts

    #: How many bytes of a name the firmware's contact record holds (a 32-byte field).
    _CONTACT_NAME_BYTES = 32

    async def add_contact(self, node: Contact) -> None:  # noqa: D102 - inherited docstring
        mc = self._require()
        pub = self._node_pubkey(node)  # raises DeviceCommandError if it has no key
        if len(pub) != 64:
            raise DeviceCommandError(
                f"{node.name} is known only by a key prefix, so it can't be added to the "
                "device — receive an advert from it first."
            )
        # The library writes contacts through one add-or-update frame, from a record shaped
        # exactly like the one a contacts read yields; a flood route (``out_path_len`` -1) is
        # what a freshly-heard contact carries, and the device relearns a path from received
        # traffic as usual.
        record = {
            "public_key": pub,
            "type": int(node.node_type if node.node_type is not None else NODE_TYPE_CHAT),
            "flags": 0,
            "out_path": "",
            "out_path_len": -1,
            "out_path_hash_mode": 0,
            "adv_name": _clip_utf8(node.name, self._CONTACT_NAME_BYTES),
            "last_advert": int(node.last_seen.timestamp()) if node.last_seen else 0,
            "adv_lat": float(node.lat or 0.0),
            "adv_lon": float(node.lon or 0.0),
        }
        result = await mc.commands.add_contact(record)
        if result is None or getattr(result, "is_error", lambda: False)():
            raise DeviceCommandError(
                f"couldn't add {node.name} to the device: {reject_reason(result)}"
            )

    async def remove_contact(self, node: Contact) -> None:  # noqa: D102 - inherited docstring
        mc = self._require()
        pub = self._node_pubkey(node)  # raises DeviceCommandError if it has no key
        result = await mc.commands.remove_contact(pub)
        if result is not None and getattr(result, "is_error", lambda: False)():
            # A contact the firmware doesn't hold is the one rejection that isn't a
            # failure: the list a screen removes from is the union of this table and the
            # contacts MeshTerm remembers for the device, so the entry the reader is
            # deleting may only ever have existed on our side. Same class the send path
            # raises, for the same reason — the caller can finish the job.
            if error_code(result) == _ERR_NOT_FOUND:
                raise ContactNotOnDeviceError(node)
            raise DeviceCommandError(
                f"couldn't remove {node.name} from the device: {reject_reason(result)}"
            )

    async def get_tx_power(self) -> int | None:  # noqa: D102 - inherited docstring
        info = await self.get_self_info()
        value = info.get("tx_power")
        return int(value) if value is not None else None

    async def set_tx_power(self, value: int) -> None:  # noqa: D102 - inherited docstring
        mc = self._require()
        # The firmware reads a signed byte; the library packs an unsigned int, which refuses a
        # negative one. Two's complement is the same low byte, so -9 dBm survives the trip.
        await mc.commands.set_tx_power(int(value) & 0xFFFFFFFF)

    @staticmethod
    def _node_pubkey(node: Contact) -> str:
        """Return a contact's full public key for remote addressing.

        Args:
            node: The contact to address.

        Returns:
            The lowercased hex public key (``0x`` stripped).

        Raises:
            DeviceCommandError: If the contact carries no public key, so it cannot be
                addressed for login/admin commands.
        """
        pub = (node.public_key or "").lower().removeprefix("0x")
        if not pub:
            raise DeviceCommandError(
                f"contact {node.name!r} has no public key on this device, so it can't be "
                "logged in to for admin commands. Receive an advert from it first."
            )
        return pub

    async def admin_login(self, node: Contact, password: str) -> LoginResult:  # noqa: D102
        from meshcore import EventType

        mc = self._require()
        pub = self._node_pubkey(node)
        loop = asyncio.get_running_loop()
        # Listen for the node's answer *before* transmitting, and keep listening across the
        # whole exchange — the rule :meth:`run_trace` follows, for the same two reasons, and
        # ``send_login_sync`` breaks both of them.
        #
        # It waits for LOGIN_SUCCESS and *only* LOGIN_SUCCESS, and it does not start waiting
        # until its own send has returned. So:
        #
        # * A refusal — which firmware does send, as a LOGIN_FAILED frame the library parses
        #   and dispatches like any other event — is never waited for, and times out exactly
        #   like an unreachable node. Both halves of failure came back as the same ``None``.
        # * Worse, for a node that is perfectly reachable: the send itself blocks until a
        #   MSG_SENT arrives, and the library correlates that acknowledgement by nothing but
        #   its event type — so a scheduled advert, a telemetry poll or the courier can
        #   consume ours and leave the send sitting on its own 15-second default. Every
        #   answer that lands during that stall is dispatched to no listener and dropped, and
        #   a login the repeater *accepted* is recorded as no reply. The mirror case is as
        #   bad: catching some other command's MSG_SENT takes its ``suggested_timeout`` with
        #   it, which for a neighbour is a second or two — nowhere near a multi-hop
        #   repeater's round trip. This is the reported bug: a healthy node, the right
        #   password, and a no-reply popup.
        #
        # Both go away by owning the wait. ``send_login_sync`` is still what transmits (it is
        # the library's supported path, and its own listener is harmless — the dispatcher
        # delivers to every matching subscription), but ours is armed first and outlives it:
        # when it gives up early, the node still gets a full budget of its own, sized to the
        # route the way a trace to the same node would be, and counted from the send rather
        # than from the queuing — see the wait below for why that distinction is the fix.
        answer: asyncio.Future = loop.create_future()
        # Frames that landed during the exchange but are not our verdict. Kept only to be
        # logged beside the outcome: a "no reply" with one of these next to it is a
        # different fault from a node that stayed silent, and nothing else would show it.
        stray: list[str] = []

        def note(what: str, event) -> None:  # noqa: ANN001 - meshcore Event
            if len(stray) < _LOGIN_STRAY_LOG_CAP:
                stray.append(f"{what} {getattr(event, 'payload', None)!r}")

        def on_answer(event) -> None:  # noqa: ANN001 - meshcore Event
            if answer.done():
                return
            if self._refers_to(event, pub):
                answer.set_result(event)
            else:
                # An answer naming some other node — two admin flows can overlap. Not ours
                # to act on, but worth saying we heard it: a login that reports silence with
                # one of these logged is a key prefix that did not match, not a quiet node.
                note("login frame for another node:", event)

        def on_local_error(event) -> None:  # noqa: ANN001 - meshcore Event
            # The one failure that never reaches the mesh: the companion refusing to send.
            # ``send_login_sync`` erases it — it turns its own ERROR into a bare ``None``,
            # indistinguishable from an acknowledgement that was merely slow — so the reason
            # is only recoverable by listening for the frame. Uncorrelated (an ERROR carries
            # no request id and may belong to another command in flight), so it is evidence
            # for the log and never a verdict.
            note("companion error:", event)

        subscriptions = [
            mc.subscribe(EventType.LOGIN_SUCCESS, on_answer),
            mc.subscribe(EventType.LOGIN_FAILED, on_answer),
            mc.subscribe(EventType.ERROR, on_local_error),
        ]
        # A login is a round trip along the contact's route and back, which is the shape a
        # trace budget already describes; the stored route is one-way, so the wire carries
        # twice its hops. Knowing the route only ever buys *more* patience, never less: an
        # admin exchange is heavier than a trace's single small packet, so a contact we hold
        # a short route for must not be given a narrower window than the routeless one
        # beside it — which is exactly what ``trace_timeout(0)``, the flood budget, is.
        hops = 2 * len(node.route_hops or ())
        budget = max(trace_timeout(hops), trace_timeout(0))
        started = loop.time()
        try:
            async with self.transmitting():
                sent = await mc.commands.send_login_sync(pub, password)
            # The budget times the *node's* answer, so it is spent from the moment the
            # request is on the air — not from the moment we began queuing it. Everything
            # before that belongs to the companion, and it can be most of a minute:
            # ``send_login_sync`` first waits its turn on the library's mesh-request lock,
            # which every telemetry poll and courier retry holds for a whole round trip, and
            # then blocks until a MSG_SENT it correlates by event type alone arrives. Timing
            # the repeater from before all that is what made the last fix a longer way of
            # failing at the same ten seconds: the wait was widened, then handed a window
            # some other command had already spent.
            sent_at = loop.time()
            if not answer.done():
                # Shielded: a timeout here must leave the future readable, not cancel it.
                try:
                    await asyncio.wait_for(asyncio.shield(answer), budget)
                except asyncio.TimeoutError:
                    pass
        finally:
            for subscription in subscriptions:
                subscription.unsubscribe()

        event = answer.result() if answer.done() else None
        if event is None and getattr(sent, "type", None) is EventType.LOGIN_SUCCESS:
            # Cannot normally happen — our subscription was registered first, so anything the
            # library's own wait saw, ours saw too. Deferring to it anyway costs nothing and
            # can only ever turn a false no-reply into the acceptance it really was, which is
            # the direction this whole method is trying to fail in.
            event = sent
        etype = getattr(event, "type", None)
        # Three numbers, because a no-reply has three different causes and they are told
        # apart only by where the time went: a send that took seconds means the request sat
        # behind another command, a send that returned at once with no acknowledgement means
        # the companion never put it on the air, and a full budget spent after a clean send
        # means the node really did stay silent.
        _log.debug(
            "admin login to %s: send %s in %.0fms, budget=%.1fs -> %s after %.0fms%s",
            node.name,
            "acknowledged" if sent is not None else "unacknowledged",
            (sent_at - started) * 1000.0,
            budget,
            etype,
            (loop.time() - sent_at) * 1000.0,
            f" [also heard: {'; '.join(stray)}]" if stray else "",
        )
        if etype is EventType.LOGIN_SUCCESS:
            return LoginResult.ACCEPTED
        if etype is EventType.LOGIN_FAILED:
            return LoginResult.REFUSED
        # Nothing came back — including the local-ERROR case, where the companion would not
        # even send the request. Either way we never heard the node, so the password stands
        # unproven rather than disproven and the caller must keep it.
        return LoginResult.NO_REPLY

    @staticmethod
    def _refers_to(event: object, pubkey: str) -> bool:
        """Is this login frame about the node we addressed?

        The firmware stamps a login reply with the *answering node's* 6-byte key prefix when
        the frame is long enough to carry one; older/terser frames arrive bare. So this
        matches when there is something to match on and accepts the frame otherwise — the
        alternative, demanding a prefix, would silently downgrade every answer from terse
        firmware into a no-reply, which is the failure this whole path exists to stop.
        """
        payload = getattr(event, "payload", None) or {}
        prefix = str(payload.get("pubkey_prefix") or "").lower().removeprefix("0x")
        return not prefix or pubkey.lower().startswith(prefix)

    async def _send_admin_cmd(self, node: Contact, cmd: str, *, timeout: float = 8.0):
        """Send a CLI command to a logged-in remote node and await its reply.

        The companion acknowledges the send immediately (``MSG_SENT``); the node's
        textual reply arrives later as a ``CONTACT_MSG_RECV`` event. We return that
        reply text (or ``None`` if none arrived before ``timeout``).

        Args:
            node: The remote contact (must already be logged in).
            cmd: The repeater CLI command, e.g. ``"set tx 20"``.
            timeout: Seconds to wait for the node's reply.

        Returns:
            The reply text, or ``None`` if the node did not answer in time.

        Raises:
            DeviceCommandError: If the companion rejected the send outright.
        """
        from meshcore import EventType

        mc = self._require()
        pub = self._node_pubkey(node)
        async with self.transmitting():
            sent = await mc.commands.send_cmd(pub, cmd)
        if sent is not None and getattr(sent, "is_error", lambda: False)():
            raise DeviceCommandError(
                f"couldn't send admin command {cmd!r} to {node.name}: {reject_reason(sent)}"
            )
        reply = await mc.wait_for_event(EventType.CONTACT_MSG_RECV, timeout=timeout)
        if reply is None:
            return None
        payload = getattr(reply, "payload", {}) or {}
        return str(payload.get("text", payload.get("msg", "")))

    async def send_remote_command(  # noqa: D102 - inherited docstring
        self, node: Contact, command: str, *, timeout: float = 8.0
    ) -> str | None:
        transmit_gate.mark()
        return await self._send_admin_cmd(node, command, timeout=timeout)

    async def get_remote_tx_power(self, node: Contact) -> int | None:  # noqa: D102
        reply = await self._send_admin_cmd(node, "get tx")
        return _parse_tx_reply(reply)

    async def set_remote_tx_power(self, node: Contact, value: int) -> None:  # noqa: D102
        # The reply ("ok"/echoed value) is best-effort confirmation; absence isn't fatal
        # since some firmware answers tersely or drops the ack under duty-cycle limits.
        await self._send_admin_cmd(node, f"set tx {value}")

    async def fetch_neighbours(self, node: Contact) -> list[NeighbourInfo]:  # noqa: D102
        mc = self._require()
        pub = self._node_pubkey(node)
        # The library pages through the table (one binary request per ~25 entries) and
        # concatenates; ``min_timeout`` keeps slow multi-hop replies from being cut off
        # at the companion's optimistic suggested timeout.
        async with self.transmitting():
            result = await mc.commands.fetch_all_neighbours(pub, min_timeout=20)
        if result is None:
            raise DeviceCommandError(
                f"{node.name!r} did not answer the neighbour request. Firmware ignores "
                "it without an admin login (log in first), and firmware older than "
                "~v1.15 has no neighbour table at all."
            )
        now = utcnow()
        neighbours: list[NeighbourInfo] = []
        for entry in result.get("neighbours") or []:
            pubkey = str(entry.get("pubkey") or "").lower()
            if not pubkey:
                continue
            snr = entry.get("snr")
            secs_ago = entry.get("secs_ago")
            heard_at = None
            if isinstance(secs_ago, (int, float)) and secs_ago >= 0:
                heard_at = now - timedelta(seconds=float(secs_ago))
            neighbours.append(
                NeighbourInfo(
                    node=pubkey,
                    snr=float(snr) if snr is not None else None,
                    heard_at=heard_at,
                )
            )
        return neighbours

    async def request_regions(self, node: Contact) -> list[str]:  # noqa: D102
        mc = self._require()
        pub = self._node_pubkey(node)
        transmit_gate.mark()
        # ``min_timeout`` for the reason the neighbour request carries one: the companion's
        # suggested timeout is optimistic for anything but an adjacent node.
        async with self.transmitting():
            text = await mc.commands.req_regions_sync(pub, min_timeout=10)
        if text is None:
            raise DeviceCommandError(
                f"{node.name!r} did not answer the regions request. A repeater answers it "
                "only when it arrives direct (a neighbour, or a contact with a route), at "
                "most every few seconds, and only on firmware 1.12 or newer."
            )
        return parse_region_list(text)

    async def run_trace(  # noqa: D102 - inherited docstring
        self,
        target: str,
        *,
        path: str | None = None,
        timeout: float | None = None,
    ) -> TraceResult:
        transmit_gate.mark()
        from meshcore import EventType  # local import keeps mock path dependency-free

        mc = self._require()
        tag = random.randint(0, 0xFFFFFFFF)
        loop = asyncio.get_running_loop()
        started = loop.time()

        # A trace packet has no destination field — it walks an explicit path of
        # repeater hops. Send the path as raw bytes so any uniform hash width
        # transmits; ``flags`` carries the path-hash mode (size - 1).
        path_bytes: bytes | None = None
        flags = 0
        if path:
            hops = [h.strip() for h in path.split(",") if h.strip()]
            path_bytes = bytes.fromhex("".join(hops))
            flags = path_hash_flags(len(bytes.fromhex(hops[0]))) or 0
        else:
            # No forced path: build a path that ends at the target's own hash
            # (prepending any learned ``out_path`` repeaters), since a trace only
            # replies when its destination is the final hop. ``None`` only when the
            # contact is unknown, leaving the trace to run path-less.
            resolved = await self._trace_path_to_contact(mc, target)
            if resolved is not None:
                path_bytes, flags = resolved

        # Size the reply-wait to the route unless the caller pinned it. ``path_bytes`` is
        # the whole walk — out plus the mirrored return leg — so its entry count (each
        # ``1 << flags`` bytes wide) is the number of relay transmissions the packet makes
        # before the reply reaches us. A path-less flood leaves the count unknown (0).
        if timeout is None:
            hops_walked = len(path_bytes) // (1 << flags) if path_bytes else 0
            timeout = trace_timeout(hops_walked)

        # Listen for our tag *before* transmitting, and keep listening for the whole
        # trace. ``send_trace`` doesn't return until the companion's ``MSG_SENT``
        # arrives, and the library correlates that acknowledgement by nothing but its
        # event type — so any other command in flight (a scheduled advert, a telemetry
        # poll, the courier) can consume ours and leave the send blocked on its own
        # 15-second default. Subscribing afterwards would mean every reply that landed
        # during that stall was dispatched to no listener and dropped, and the trace
        # recorded as "no reply" though the mesh answered it — which is why a burst of
        # background traffic used to fail *every* trace for as long as it lasted, not
        # just the one it collided with. The reply's own arrival time is stamped in the
        # handler so a stalled send inflates no round trip.
        reply: asyncio.Future = loop.create_future()
        landed = started

        def on_trace_reply(event) -> None:  # noqa: ANN001 - meshcore Event
            nonlocal landed
            if not reply.done():
                landed = loop.time()
                reply.set_result(event)

        subscription = mc.subscribe(EventType.TRACE_DATA, on_trace_reply, {"tag": tag})
        try:
            sent = await mc.commands.send_trace(auth_code=0, tag=tag, flags=flags, path=path_bytes)
            if getattr(sent, "is_error", None) is not None and sent.is_error():
                # Uncorrelated acknowledgements make this ambiguous — the error may
                # belong to another command entirely — so it is evidence, not a verdict:
                # the trace is still on the air and its reply may yet arrive.
                _log.debug("trace %08x: send reported %s", tag, sent.payload)
            try:
                event = await asyncio.wait_for(reply, timeout)
            except asyncio.TimeoutError:
                event = None
        finally:
            subscription.unsubscribe()
        elapsed_ms = (landed - started) * 1000.0
        _log.debug(
            "trace %08x: path=%s flags=%d timeout=%.1fs -> %s",
            tag,
            path_bytes.hex() if path_bytes else "(none)",
            flags,
            timeout,
            f"{elapsed_ms:.0f}ms" if event is not None else "no reply",
        )
        # The firmware addresses each hop by a hash of ``1 << flags`` bytes; record it
        # so the summary can show node hashes at the width the command actually used.
        hash_bytes = 1 << flags
        if event is None:
            return TraceResult(
                target=target, success=False, round_trip_ms=None, path_hash_bytes=hash_bytes
            )

        payload = getattr(event, "payload", {}) or {}
        hops = parse_trace_hops(payload)
        return TraceResult(
            target=target,
            success=True,
            hops=hops,
            round_trip_ms=elapsed_ms,
            tx_power=await self.get_tx_power(),
            path_hash_bytes=hash_bytes,
            raw=payload,
        )

    async def _trace_path_to_contact(self, mc, target: str) -> tuple[bytes, int] | None:  # noqa: ANN001
        """Resolve a target contact into a trace ``(path_bytes, flags)``.

        A trace reply only comes back when the *destination's own hash* is the final
        hop in the path — an empty/destination-less path is silently dropped (verified
        on hardware: a direct neighbor answers a single-hop trace to its own hash but
        not a path-less one). So we always end the outbound leg at the contact's key
        prefix, prepending any learned repeater hops (``out_path``) ahead of it — and,
        since the trace protocol has no separate return-path field, mirror those same
        repeaters back afterwards (see :func:`~meshterm.services.topology.render_forced_spec`,
        which does the same for a composed/adopted path): without an explicit return
        leg the repeaters have nothing to relay the reply through, so it never comes
        home.

        * direct neighbor / no learned route → just ``[destination]`` (no repeaters
          to mirror, so the outbound leg is the whole path);
        * learned multi-hop route → ``[repeater…, destination, repeater… (reversed)]``.

        Each hash is re-encoded at the trace's own width (``1 << flags``, only 1/2/4/8
        bytes), collapsing a region's routing width (e.g. 3) to the widest representable
        value (2). The firmware matches by hash prefix, so a narrower prefix still
        addresses the same node.

        Args:
            mc: The connected ``MeshCore`` client.
            target: Contact name (case-insensitive) or public-key prefix.

        Returns:
            ``(path_bytes, flags)`` to walk, or ``None`` only when the contact is
            unknown or carries no public key to address.
        """
        payload = await self._contacts_payload(mc)
        needle = target.casefold()
        for name, info in payload.items():
            info = info or {}
            adv = info.get("adv_name", name)
            pub = (info.get("public_key", "") or "").lower().removeprefix("0x")
            if adv.casefold() != needle and not pub.startswith(needle):
                continue
            if not pub:
                return None  # no key to address the trace's destination hop

            # Routing hash width (bytes): the contact's stored mode, or — when it has
            # no learned route (mode == -1) — our region's mode.
            mode = int(info.get("out_path_hash_mode", -1))
            if mode < 0:
                try:
                    mode = int(await mc.commands.get_path_hash_mode())
                except Exception:
                    mode = 2  # region default: 3-byte hashes
            size = max(mode + 1, 1)
            trace_size = max(s for s in (1, 2, 4, 8) if s <= size)

            repeaters: list[bytes] = []
            out_path = (info.get("out_path") or "").strip().lower().removeprefix("0x")
            out_path_len = int(info.get("out_path_len", -1))
            if 1 <= out_path_len <= 254 and out_path:
                # Learned multi-hop route: walk each repeater, collapsed to trace width.
                route = bytes.fromhex(out_path)[: out_path_len * size]
                repeaters = [route[i * size : i * size + trace_size] for i in range(out_path_len)]
            dest = bytes.fromhex(pub)[:trace_size]
            # The outbound leg always finishes at the destination's own hash so it
            # recognizes the trace and replies; the return leg mirrors the same
            # repeaters back to us, since nothing reflects the packet automatically.
            path_bytes = b"".join(repeaters) + dest + b"".join(reversed(repeaters))
            return path_bytes, path_hash_flags(trace_size) or 0
        return None

    async def subscribe_events(  # noqa: D102 - inherited docstring
        self, on_event: EventCallback
    ) -> Unsubscribe:
        from meshcore import EventType

        mc = self._require()
        subscribe = getattr(mc, "subscribe", None)
        if subscribe is None:  # pragma: no cover - depends on installed meshcore build
            raise DeviceCommandError(
                "this meshcore build doesn't expose event subscription, so passive "
                "monitoring isn't available. Upgrade the 'meshcore' library."
            )

        def observation_handler(kind: str):  # type: ignore[no-untyped-def]
            def handler(event) -> None:  # noqa: ANN001
                obs = observation_from_event(event, kind)
                if obs is not None:
                    on_event(MeshEvent.observation_event(obs))

            return handler

        def message_handler(event) -> None:  # noqa: ANN001
            msg = message_from_event(event)
            if msg is not None:
                on_event(MeshEvent.message_event(msg))

        def ack_handler(event) -> None:  # noqa: ANN001
            on_event(MeshEvent.ack_event(ack_from_event(event)))

        def packet_handler(event) -> None:  # noqa: ANN001
            obs = packet_observation_from_event(event)
            if obs is not None:
                on_event(MeshEvent.observation_event(obs))

        # Subscribe to whichever event types this firmware/library build exposes. The
        # event payload field names the mappers read are best-effort and, like the trace
        # mapping, should be validated against your firmware's event schema.
        subs = []
        for attr, kind in (
            ("ADVERTISEMENT", "advert"),
            ("ADVERT", "advert"),
            ("NEW_CONTACT", "advert"),
            ("TELEMETRY_RESPONSE", "telemetry"),
        ):
            etype = getattr(EventType, attr, None)
            if etype is not None:
                subs.append(subscribe(etype, observation_handler(kind)))
        # The companion's RX packet log, when its firmware has packet logging enabled:
        # every overheard frame arrives with the relay path it traversed — the passive
        # topology evidence the trace path composer suggests hops from. Firmware without
        # RX logging simply never pushes these; subscribing is free either way.
        etype = getattr(EventType, "RX_LOG_DATA", None)
        if etype is not None:
            subs.append(subscribe(etype, packet_handler))
        for attr in ("CONTACT_MSG_RECV", "CHANNEL_MSG_RECV"):
            etype = getattr(EventType, attr, None)
            if etype is not None:
                subs.append(subscribe(etype, message_handler))
        etype = getattr(EventType, "ACK", None)
        if etype is not None:
            subs.append(subscribe(etype, ack_handler))
        # Trace replies aren't observations — the issuing ``run_trace`` consumes them by
        # tag — but logging every one that lands is what makes a "no reply" diagnosable:
        # a reply logged here with no matching ``trace <tag>`` line means the walk came
        # home and we weren't listening, which is a different fault from silence on air.
        etype = getattr(EventType, "TRACE_DATA", None)
        if etype is not None:
            subs.append(
                subscribe(
                    etype,
                    lambda event: _log.debug(
                        "trace reply heard: tag=%08x",
                        (getattr(event, "payload", {}) or {}).get("tag", 0),
                    ),
                )
            )

        # Drive the inbound-message pull ourselves (see ``_message_pump``): MeshCore never
        # pushes message bodies, so without this sending works but nothing is received.
        stop_pump = self._message_pump(mc, subs, subscribe)

        def unsubscribe() -> None:
            stop_pump()
            for sub in subs:
                unsub = getattr(sub, "unsubscribe", None)
                if unsub is not None:
                    try:
                        unsub()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass

        return unsubscribe

    def _message_pump(self, mc, subs: list, subscribe) -> Unsubscribe:  # type: ignore[no-untyped-def]
        """Continuously pull inbound messages from the companion (the RX pull model).

        MeshCore doesn't push message bodies unsolicited: the device raises a
        ``MESSAGES_WAITING`` notification and the client must call ``get_msg()`` to retrieve
        each queued message, which the library's reader then dispatches as
        ``CONTACT_MSG_RECV`` / ``CHANNEL_MSG_RECV`` to the handler registered above (a
        command's own temporary listener does not consume the event — every subscriber
        still sees it). We drive that pull three ways so it is robust across firmware
        builds: an immediate drain (delivers anything already queued), a drain on each
        ``MESSAGES_WAITING`` push (low latency), and a slow timer (a safety net for builds
        whose pushes are unreliable — the failure this fixes). Drains are serialized by a
        lock so the overlapping triggers never issue concurrent ``get_msg`` commands.

        Args:
            mc: The connected ``MeshCore`` client.
            subs: The subscription list to append the ``MESSAGES_WAITING`` sub to (so it is
                torn down with the others).
            subscribe: The client's ``subscribe`` callable.

        Returns:
            A zero-argument callable that stops the pump (its poll task and drains).
        """
        from meshcore import EventType

        stop = asyncio.Event()
        draining = asyncio.Lock()

        async def drain() -> None:
            # Pull until the device reports the queue is empty; each retrieved message is
            # delivered to our handler by the reader's dispatch, so there's nothing to do
            # with the returned event but check whether to keep going.
            async with draining:
                while not stop.is_set():
                    try:
                        event = await mc.commands.get_msg(timeout=_MESSAGE_GET_TIMEOUT_S)
                    except Exception as exc:  # noqa: BLE001 - transient; the poll retries
                        _log.debug("message pump: get_msg failed: %s", exc)
                        return
                    etype = getattr(event, "type", None)
                    if event is None or etype in (EventType.NO_MORE_MSGS, EventType.ERROR):
                        return

        def schedule_drain(_event=None) -> None:  # noqa: ANN001 - MESSAGES_WAITING callback
            asyncio.ensure_future(drain())

        async def poll_loop() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), _MESSAGE_POLL_INTERVAL_S)
                except asyncio.TimeoutError:
                    await drain()  # interval elapsed; sweep for anything the push missed

        waiting = getattr(EventType, "MESSAGES_WAITING", None)
        if waiting is not None:
            subs.append(subscribe(waiting, schedule_drain))
        schedule_drain()  # immediate initial drain of anything already queued
        poll_task = asyncio.ensure_future(poll_loop())

        def stop_pump() -> None:
            stop.set()  # ends the poll loop and any in-flight drain at the next check
            poll_task.cancel()

        return stop_pump

    async def send_direct_message(  # noqa: D102 - inherited docstring
        self, contact: Contact, text: str
    ) -> Ack | None:
        transmit_gate.mark()
        from meshcore import EventType

        mc = self._require()
        pub = self._node_pubkey(contact)
        # Only the hand-over holds the transmit lock; the ack wait below is not a send.
        async with self.transmitting():
            result = await mc.commands.send_msg(pub, text)
        if result is None or getattr(result, "is_error", lambda: False)():
            # A recipient the firmware has no entry for is the one rejection with an obvious
            # fix, so it gets its own class for the chat screen to offer that fix on; see
            # :class:`ContactNotOnDeviceError` for how a listed contact can be missing here.
            if error_code(result) == _ERR_NOT_FOUND:
                raise ContactNotOnDeviceError(contact)
            raise DeviceCommandError(f"couldn't send to {contact.name}: {reject_reason(result)}")
        # The companion acknowledges the send immediately with an ``expected_ack`` code and
        # a suggested wait; the recipient's delivery ACK arrives later carrying that code.
        payload = getattr(result, "payload", {}) or {}
        expected = payload.get("expected_ack")
        expected_hex = expected.hex() if isinstance(expected, (bytes, bytearray)) else expected
        if not expected_hex:
            return None
        suggested = payload.get("suggested_timeout")
        timeout = suggested / 1000 * 1.2 if suggested else 8.0
        ack = await mc.wait_for_event(
            EventType.ACK, attribute_filters={"code": expected_hex}, timeout=timeout
        )
        return ack_from_event(ack) if ack is not None else None

    async def send_channel_message(  # noqa: D102 - inherited docstring
        self, index: int, text: str
    ) -> None:
        transmit_gate.mark()
        async with self.transmitting():
            self._ok(await self._require().commands.send_chan_msg(index, text))

    @staticmethod
    def _ok(event):  # type: ignore[no-untyped-def]
        """Return ``event`` if it succeeded, else raise its error payload.

        Args:
            event: The :class:`meshcore.events.Event` returned by a command.

        Returns:
            The same event, for convenient chaining.

        Raises:
            RuntimeError: If the device reported an error.
        """
        if event is not None and getattr(event, "is_error", lambda: False)():
            raise RuntimeError(f"device rejected the command: {reject_reason(event)}")
        return event

    async def get_tuning(self) -> dict:  # noqa: D102 - inherited docstring
        event = self._ok(await self._require().commands.get_tuning())
        payload = getattr(event, "payload", {}) or {}
        # The firmware stores floats and reports them ×1000 (rx_delay_base * 1000,
        # airtime_factor * 1000); undo that so callers see the real values.
        return {
            "rx_delay": int(payload.get("rx_delay", 0)) / 1000.0,
            "airtime_factor": int(payload.get("airtime_factor", 0)) / 1000.0,
        }

    #: The hop limit that arrived in the same frame as the last bitmask, until it is taken.
    _autoadd_hops: object = _UNREAD

    async def get_autoadd_config(self) -> int | None:  # noqa: D102 - inherited docstring
        mc = self._require()
        # One reply carries the bitmask *and* the hop limit, but the library's parser keeps
        # only the first. So the reader is tapped for the length of this one read — every
        # transport hands frames to ``reader.handle_rx`` by attribute lookup, so an instance
        # attribute sees them first — and the raw frame's second byte is kept.
        reader = getattr(mc, "_reader", None)
        frames: list[bytes] = []
        tapped = reader is not None and "handle_rx" not in vars(reader)
        if tapped:
            original = reader.handle_rx

            async def tap(data: bytearray) -> None:
                if data and data[0] == _RESP_AUTOADD_CONFIG:
                    frames.append(bytes(data))
                await original(data)

            reader.handle_rx = tap
        try:
            event = self._ok(await mc.commands.get_autoadd_config())
        finally:
            if tapped:
                del reader.handle_rx
        payload = getattr(event, "payload", {}) or {}
        config = payload.get("config")
        frame = frames[-1] if frames else b""
        self._autoadd_hops = frame[2] if len(frame) >= 3 else None
        return None if config is None else int(config)

    async def get_autoadd_max_hops(self) -> int | None:  # noqa: D102 - inherited docstring
        # build_snapshot asks for the bitmask and then for this. The frame that answered the
        # first question already carried the second, so it is taken rather than asked again.
        if self._autoadd_hops is _UNREAD:
            await self.get_autoadd_config()
        hops, self._autoadd_hops = self._autoadd_hops, _UNREAD
        return hops  # type: ignore[return-value]

    async def get_allowed_repeat_freqs(self) -> list[tuple[float, float]]:  # noqa: D102
        event = self._ok(await self._require().commands.get_allowed_repeat_freq())
        payload = getattr(event, "payload", {}) or {}
        # Ranges travel in kHz, like every other frequency on this protocol.
        return [
            (int(r["min"]) / 1000.0, int(r["max"]) / 1000.0) for r in payload.get("freqs") or []
        ]

    async def get_default_flood_scope(self) -> str | None:  # noqa: D102
        event = self._ok(await self._require().commands.get_default_flood_scope())
        payload = getattr(event, "payload", {}) or {}
        name = payload.get("scope_name")
        # Bare, whichever client wrote it: MeshTerm before its own framing, and the library
        # still, stored a ``#`` the firmware and the apps leave off.
        return None if name is None else normalize_region(str(name))

    async def get_time(self) -> int | None:  # noqa: D102 - inherited docstring
        event = self._ok(await self._require().commands.get_time())
        payload = getattr(event, "payload", {}) or {}
        value = payload.get("time")
        return None if value is None else int(value)

    async def get_battery(self) -> dict:  # noqa: D102 - inherited docstring
        event = self._ok(await self._require().commands.get_bat())
        return dict(getattr(event, "payload", {}) or {})

    async def get_hw_charging(self) -> bool | None:  # noqa: D102 - inherited docstring
        # BLE only, and only when a device exposes the standard Battery Level Status
        # characteristic — no MeshCore firmware does today, so this returns None on every
        # current device and the battery poller falls back to its voltage-trend inference. It
        # reaches the raw bleak client the way the forced-teardown path does (through
        # ``connection_manager.connection``) and is wrapped whole: any failure — attribute
        # path moved, service absent, read refused, short value — is a quiet None, never a
        # disrupted poll.
        if self._transport != "ble" or self._mc is None:
            return None
        try:
            client = self._mc.connection_manager.connection.client
            service = client.services.get_service(_BATTERY_SERVICE_UUID)
            if service is None:
                return None
            char = service.get_characteristic(_BATTERY_LEVEL_STATUS_UUID)
            if char is None or "read" not in getattr(char, "properties", ()):
                return None
            value = await client.read_gatt_char(char)
        except Exception as exc:  # noqa: BLE001 - a best-effort probe must never disrupt polling
            _log.debug("hardware charging read failed: %s", exc)
            return None
        if not self._logged_bas:
            self._logged_bas = True
            _log.info(
                "device exposes a standard BLE Battery Service (0x180F); using its "
                "charging flag over the voltage-trend inference — verify the reading"
            )
        return charging_from_battery_level_status(bytes(value))

    async def get_stats(self) -> dict:  # noqa: D102 - inherited docstring
        mc = self._require()
        stats: dict = {}
        # Each frame independently best-effort: firmware predating one stats type answers
        # with an error, which must not cost us the frames it does support.
        for read in (
            mc.commands.get_stats_core,
            mc.commands.get_stats_radio,
            mc.commands.get_stats_packets,
        ):
            try:
                event = self._ok(await read())
            except Exception:  # noqa: BLE001 - optional read; absence is acceptable
                continue
            stats.update(getattr(event, "payload", {}) or {})
        return stats

    async def get_path_hash_mode(self) -> int:  # noqa: D102 - inherited docstring
        return int(await self._require().commands.get_path_hash_mode())

    async def get_custom_vars(self) -> dict[str, str]:  # noqa: D102 - inherited docstring
        event = self._ok(await self._require().commands.get_custom_vars())
        return dict(getattr(event, "payload", {}) or {})

    async def get_channel(self, index: int) -> dict | None:  # noqa: D102
        # The read is serialized (see self._channel_read_lock) so the uncorrelated
        # CHANNEL_INFO response can't be stolen by a concurrent read. We still verify the
        # response is for the slot we asked about: a stray CHANNEL_INFO (from another source,
        # or a slow response arriving after a timeout) would otherwise misidentify the slot and
        # misfile a channel's messages. On mismatch we raise rather than return foreign data.
        async with self._channel_read_lock:
            event = await self._channel_event(index)
        payload = getattr(event, "payload", {}) or {}
        got = payload.get("channel_idx")
        if got is not None and int(got) != index:
            raise DeviceCommandError(f"channel read for slot {index} returned slot {got}")
        if not payload.get("channel_name"):
            return None
        return payload

    async def _channel_event(self, index: int):  # noqa: ANN202 - a meshcore Event
        """Read one slot, telling the firmware's "no such slot" apart from any other error.

        The slot probe ends on a *rejection* and keeps the list it has as the device's whole
        layout, so what counts as one matters. The firmware answers an index past its last
        slot with ``ERR_CODE_NOT_FOUND`` and nothing else. But the library does not
        serialize commands, and every one that waits for "OK or ERROR" takes the first
        ERROR that arrives — so a read racing another command can be handed *that*
        command's refusal. At connect the channel prewarm runs beside the clock sync, and a
        clock the firmware refuses as malformed ended the probe at whatever slot it had
        reached, which cached a list missing every channel after it for the session.

        So only ``NOT_FOUND`` (or a code-less refusal from firmware too old to send one) is
        a rejection. Any other refusal is asked again once, since a borrowed error is
        someone else's and the next answer is this read's own; a second one, or no answer
        at all, is a failed read (:class:`DeviceCommandError`), which the probe reports as
        unfinished rather than caching.
        """
        commands = self._require().commands
        for attempt in (1, 2):
            event = await commands.get_channel(index)
            if event is None or not getattr(event, "is_error", lambda: False)():
                return event
            payload = getattr(event, "payload", {}) or {}
            if isinstance(payload, dict) and payload.get("reason") == "no_event_received":
                raise DeviceCommandError(f"channel read for slot {index} got no answer")
            code = error_code(event)
            if code is None or code == _ERR_NOT_FOUND:
                return self._ok(event)  # the firmware's own "no such slot"
            if attempt == 2:
                raise DeviceCommandError(
                    f"channel read for slot {index} was refused: {reject_reason(event)}"
                )
        return None  # pragma: no cover - the loop always returns or raises

    async def set_name(self, name: str) -> None:  # noqa: D102 - inherited docstring
        self._ok(await self._require().commands.set_name(name))

    async def set_coords(self, lat: float, lon: float) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_coords(lat, lon))

    async def set_device_pin(self, pin: int) -> None:  # noqa: D102 - inherited docstring
        self._ok(await self._require().commands.set_devicepin(pin))

    async def set_radio(  # noqa: D102 - inherited docstring
        self, freq: float, bw: float, sf: int, cr: int, repeat: bool | None = None
    ) -> None:
        self._ok(
            await self._require().commands.set_radio(
                freq, bw, sf, cr, None if repeat is None else int(bool(repeat))
            )
        )

    async def set_tuning(self, rx_delay: float, airtime_factor: float) -> None:  # noqa: D102
        # CMD_SET_TUNING_PARAMS carries both floats ×1000; the firmware divides them
        # back out (prefs.rx_delay_base = rx / 1000, prefs.airtime_factor = af / 1000).
        self._ok(
            await self._require().commands.set_tuning(
                round(float(rx_delay) * 1000), round(float(airtime_factor) * 1000)
            )
        )

    async def set_autoadd_config(  # noqa: D102 - inherited docstring
        self, flags: int, max_hops: int | None = None
    ) -> None:
        from meshcore import EventType

        mc = self._require()
        if max_hops is None:
            self._ok(await mc.commands.set_autoadd_config(int(flags)))
        else:
            # The library sends the bitmask alone; the hop limit is the byte after it.
            frame = bytes([_CMD_SET_AUTOADD_CONFIG, int(flags) & 0xFF, min(int(max_hops), 64)])
            self._ok(await mc.commands.send(frame, [EventType.OK, EventType.ERROR]))
        self._autoadd_hops = _UNREAD

    async def set_default_flood_scope(self, scope: str) -> None:  # noqa: D102
        from meshcore import EventType

        # Framed here rather than by the library's ``set_default_flood_scope``, which stores
        # the name *with* a ``#`` (the firmware and the apps store it bare, so ours read
        # back differently from theirs, and a 30-byte name became a 31-byte one the
        # firmware refuses) and pads by characters rather than UTF-8 bytes (so a name with
        # an accent put the key at the wrong offset). ``[63][name, 31 bytes NUL-padded]
        # [key16]``; a frame shorter than that clears the default.
        bare = normalize_region(scope)
        if not bare or bare == REGION_WILDCARD:
            frame = bytes([_CMD_SET_DEFAULT_FLOOD_SCOPE])
        else:
            bare = validate_region(bare)
            name = bare.encode("utf-8")
            frame = (
                bytes([_CMD_SET_DEFAULT_FLOOD_SCOPE])
                + name
                + bytes(31 - len(name))
                + region_key(bare)
            )
        mc = self._require()
        self._ok(await mc.commands.send(frame, [EventType.OK, EventType.ERROR]))

    async def set_flood_scope(self, region: str | None) -> None:  # noqa: D102
        from meshcore import EventType

        # Framed here, like the default scope, rather than through the library's helpers:
        # ``reset_flood_scope`` and ``force_unscoped`` arrived late in meshcore 2.3.x, and
        # an install that satisfies ``meshcore>=2.3`` without them (the uConsole's 2.3.7)
        # crashed every scoped channel send on the restore. The frame is the firmware's
        # own ``CMD_SET_FLOOD_SCOPE_KEY``: ``[54][0][key16]`` sets the session scope,
        # ``[54][0]`` alone clears it back to the default, ``[54][1]`` forces unscoped.
        bare = normalize_region(region) if region else ""
        if not bare:
            frame = bytes([_CMD_SET_FLOOD_SCOPE_KEY, 0])
        elif bare == REGION_WILDCARD:
            frame = bytes([_CMD_SET_FLOOD_SCOPE_KEY, 1])
        else:
            frame = bytes([_CMD_SET_FLOOD_SCOPE_KEY, 0]) + region_key(validate_region(bare))
        mc = self._require()
        self._ok(await mc.commands.send(frame, [EventType.OK, EventType.ERROR]))

    async def set_manual_add_contacts(self, enabled: bool) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_manual_add_contacts(enabled))

    async def set_adv_loc_policy(self, policy: int) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_advert_loc_policy(policy))

    async def set_multi_acks(self, value: int) -> None:  # noqa: D102 - inherited docstring
        self._ok(await self._require().commands.set_multi_acks(value))

    async def set_telemetry_modes(self, base: int, loc: int, env: int) -> None:  # noqa: D102
        mc = self._require()
        result = self._ok(await mc.commands.send_appstart())
        infos = dict(getattr(result, "payload", {}) or {})
        infos["telemetry_mode_base"] = base
        infos["telemetry_mode_loc"] = loc
        infos["telemetry_mode_env"] = env
        self._ok(await mc.commands.set_other_params_from_infos(infos))

    async def set_path_hash_mode(self, mode: int) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_path_hash_mode(mode))

    async def set_custom_var(self, key: str, value: str) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_custom_var(key, value))

    async def set_channel(self, index: int, name: str, secret: bytes | None) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_channel(index, name, secret))

    async def set_time(self, epoch: int) -> None:  # noqa: D102 - inherited docstring
        self._ok(await self._require().commands.set_time(epoch))

    async def send_advert(self, flood: bool = False) -> None:  # noqa: D102
        transmit_gate.mark(flood_advert=flood)
        async with self.transmitting():
            self._ok(await self._require().commands.send_advert(flood))

    async def reboot(self) -> None:  # noqa: D102 - inherited docstring
        await self._require().commands.reboot()  # device reboots; no OK reply expected

    async def export_private_key(self) -> str:  # noqa: D102 - inherited docstring
        event = self._ok(await self._require().commands.export_private_key())
        payload = getattr(event, "payload", {}) or {}
        key = payload.get("private_key")
        if key is None:
            raise RuntimeError("device did not return a private key (export may be disabled)")
        return key.hex() if isinstance(key, (bytes, bytearray)) else str(key)

    async def import_private_key(self, key_hex: str) -> None:  # noqa: D102
        self._ok(await self._require().commands.import_private_key(bytes.fromhex(key_hex)))

    async def factory_reset(self) -> None:  # noqa: D102 - inherited docstring
        mc = self._require()
        token = await mc.commands.request_factory_reset()
        self._ok(await mc.commands.confirm_factory_reset(token))


class MockDevice(Device):
    """A deterministic simulator implementing the full :class:`Device` interface.

    The simulated SNR follows an inverted-U response to TX power: too low and the signal
    sits in the noise floor, too high and the receiver saturates. This gives the TX
    optimizer a realistic, unimodal-with-noise curve to converge on without hardware.

    Attributes:
        optimal_tx: The TX power at which the simulated link peaks.
    """

    def __init__(
        self,
        seed: int = 1234,
        optimal_tx: int = 14,
        optimal_remote_tx: int = 20,
        admin_password: str = "admin",
    ) -> None:
        """Initialize the simulator.

        Args:
            seed: RNG seed for reproducible measurement noise.
            optimal_tx: Local TX power at which the simulated *bottleneck* SNR peaks.
            optimal_remote_tx: Remote-node TX power at which the simulated SNR *at the
                target* peaks (what the remote-admin optimizer converges on).
            admin_password: Password the simulated remote nodes accept for admin login.
        """
        self.optimal_tx = optimal_tx
        self.optimal_remote_tx = optimal_remote_tx
        self._admin_password = admin_password
        self._rng = random.Random(seed)
        self._tx_power = 20
        self._connected = False
        # Routes mirror what real firmware learns from received floods: the repeaters are
        # direct neighbours, the leaf nodes sit one hop behind one of them — so the trace
        # path composer and its topology suggestions are fully exercisable without radio.
        self._contacts = [
            Contact(
                name="Yagi-Repeater",
                public_key=_mock_pub("a1b2c3d4"),
                key_prefix="a1b2c3d4",
                node_type=NODE_TYPE_REPEATER,
                lat=45.5019,
                lon=-73.5674,
                route_hops=(),
            ),
            Contact(
                name="Local-Repeater",
                public_key=_mock_pub("b2c3d4e5"),
                key_prefix="b2c3d4e5",
                node_type=NODE_TYPE_REPEATER,
                lat=45.4768,
                lon=-73.5990,
                route_hops=(),
            ),
            Contact(
                name="Observer-Bot",
                public_key=_mock_pub("c3d4e5f6"),
                key_prefix="c3d4e5f6",
                node_type=NODE_TYPE_CHAT,
                lat=45.4880,
                lon=-73.5810,
                route_hops=("b2c3d4e5",),
            ),
            Contact(
                name="Alice",
                public_key=_mock_pub("d4e5f6a7"),
                key_prefix="d4e5f6a7",
                node_type=NODE_TYPE_CHAT,
                route_hops=("a1b2c3d4",),
            ),
        ]
        # Remote-admin simulation: which nodes we're "logged in" to, and each tuned
        # node's transmit power keyed by full public key. ``_default_remote_tx`` is the
        # assumed power before the optimizer first writes one.
        self._admin_sessions: set[str] = set()
        # Nodes the simulator answers *nothing* for — the one failure a password cannot
        # explain. Empty by default, so the simulated mesh is fully reachable as before;
        # put a contact's name in here to walk the down-repeater path, where a login comes
        # back NO_REPLY and the remembered credential has to survive it.
        self._unreachable: set[str] = set()
        self._remote_tx: dict[str, int] = {}
        self._default_remote_tx = 20
        # Each simulated repeater's CLI-visible configuration, populated with the
        # defaults below on first touch (keyed by full public key, like the TX map).
        self._remote_cfg: dict[str, dict[str, str]] = {}
        # Simulated neighbour tables, keyed by the repeater's key prefix: what each
        # repeater "hears directly" as ``(neighbour_prefix, snr_db, secs_ago)``. The
        # ``e5f6a7b8`` entry is deliberately absent from the contact list, so the
        # fetched-evidence flow exercises discovering a node we never received from.
        self._neighbour_tables: dict[str, list[tuple[str, float, int]]] = {
            "a1b2c3d4": [
                ("d4e5f6a7", 6.5, 300),
                ("b2c3d4e5", -3.25, 1200),
                ("e5f6a7b8", 2.0, 3600),
            ],
            "b2c3d4e5": [
                ("c3d4e5f6", 8.0, 240),
                ("a1b2c3d4", -3.25, 900),
            ],
        }
        # Simulated region tables, keyed like the neighbour tables: one per repeater, what
        # its ``region`` CLI edits and the anonymous regions request reads. Yagi's fits in
        # one reply; Local's is deliberately too big for the 160-byte reply buffer, so the
        # region editor's cut-and-recover path is drivable without hardware.
        self._region_maps: dict[str, SimulatedRegionMap] = {
            "a1b2c3d4": SimulatedRegionMap(
                [
                    ("lakeside", "*", True),
                    ("lakeside-north", "lakeside", True),
                    ("lakeside-south", "lakeside", False),
                    ("harbour", "*", True),
                ]
            ),
            "b2c3d4e5": SimulatedRegionMap(
                [
                    ("lakeside", "*", True),
                    ("lakeside-north", "lakeside", True),
                    ("lakeside-south", "lakeside", False),
                    ("harbour", "*", True),
                    ("harbour-east", "harbour", True),
                    ("harbour-west", "harbour", True),
                    ("old-town", "*", False),
                    ("old-town-market", "old-town", True),
                    ("riverside", "*", True),
                    ("riverside-upper", "riverside", True),
                    ("riverside-lower", "riverside", False),
                    ("hilltop", "*", True),
                ]
            ),
        }
        # Mutable configuration state, keyed exactly like the real SELF_INFO payload so the
        # settings registry behaves identically on the simulator and on hardware.
        self._info: dict = {
            "name": "MockCompanion",
            "adv_type": 1,
            "max_tx_power": 22,
            "public_key": "00" * 32,
            "adv_lat": 0.0,
            "adv_lon": 0.0,
            "multi_acks": 0,
            "adv_loc_policy": 0,
            "telemetry_mode_base": 0,
            "telemetry_mode_loc": 0,
            "telemetry_mode_env": 0,
            "manual_add_contacts": False,
            # The firmware's own build defaults (MeshCore's platformio.ini: LORA_FREQ
            # 869.618, BW 62.5, SF8; CR falls back to 5), so the simulator boots on the
            # settings a freshly flashed companion does rather than on the 250 kHz /
            # SF11 modulation the EU mesh left behind.
            "radio_freq": 869.618,
            "radio_bw": 62.5,
            "radio_sf": 8,
            "radio_cr": 5,
            "simulated": True,
        }
        self._tuning: dict = {"rx_delay": 0.0, "airtime_factor": 0.0}
        self._autoadd_config = 0
        self._autoadd_max_hops = 0
        # Client repeat (firmware v9+): off, as a freshly flashed companion ships.
        self._client_repeat = False
        self._flood_scope = ""
        #: The session send scope (``""`` follows the default, ``"*"`` is forced unscoped).
        self._send_scope = ""
        #: Every channel message handed over: ``(slot, text, session scope at the time)``.
        self.sent_channel: list[tuple[int, str, str]] = []
        # Simulated clock skew (seconds behind the host), so the sync-clock flow has a
        # visible drift to correct until set_time is called.
        self._clock_offset: int | None = -125
        self._path_hash_mode = 0
        self._custom_vars: dict[str, str] = {}
        self._channels: dict[int, dict] = {}
        # Model the firmware's fixed slot count: reads past it are rejected, exactly as a
        # real device signals its ceiling (so ``channel_capacity`` discovers 8 on the mock).
        self._max_channels = 8
        self._device_pin = 0
        self._private_key = "11" * 32
        # Background emitter tasks spawned by ``subscribe_events``; tracked so they
        # can be cancelled on disconnect and are never garbage-collected while pending.
        self._bg_tasks: set[asyncio.Task] = set()

    async def connect(self) -> None:  # noqa: D102 - inherited docstring
        await asyncio.sleep(0)
        self._connected = True

    async def disconnect(self) -> None:  # noqa: D102 - inherited docstring
        self._connected = False
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()

    async def get_self_info(self) -> dict:  # noqa: D102 - inherited docstring
        return {**self._info, "tx_power": self._tx_power}

    async def get_device_info(self) -> dict:  # noqa: D102 - inherited docstring
        # The simulator reports a stable, obviously-synthetic model so the hardware column
        # renders identically to a real board without pretending to be one. ``ble_pin``
        # rides here rather than in SELF_INFO because that is where real firmware puts it.
        return {
            "model": "MeshCore Simulator",
            "ver": "mock",
            "fw_build": "mock",
            "ble_pin": self._device_pin,
            "repeat": self._client_repeat,
        }

    async def get_contacts(self) -> list[Contact]:  # noqa: D102 - inherited docstring
        return list(self._contacts)

    async def add_contact(self, node: Contact) -> None:  # noqa: D102 - inherited docstring
        await asyncio.sleep(0)
        key = self._mock_key(node)
        self._contacts = [c for c in self._contacts if self._mock_key(c) != key]
        # Added with no learned route, exactly as the firmware stores a contact it was
        # handed rather than heard from.
        self._contacts.append(replace(node, route_hops=None))

    async def remove_contact(self, node: Contact) -> None:  # noqa: D102 - inherited docstring
        await asyncio.sleep(0)
        key = self._mock_key(node)
        # Firmware can only delete a contact it holds, and answers ERR_CODE_NOT_FOUND for
        # one it doesn't; the simulator refuses the same way so the "wasn't on the device,
        # removed here anyway" path is walkable without a radio.
        if key not in {self._mock_key(c) for c in self._contacts}:
            raise ContactNotOnDeviceError(node)
        self._contacts = [c for c in self._contacts if self._mock_key(c) != key]

    async def get_tx_power(self) -> int | None:  # noqa: D102 - inherited docstring
        return self._tx_power

    async def set_tx_power(self, value: int) -> None:  # noqa: D102 - inherited docstring
        self._tx_power = value

    async def send_direct_message(  # noqa: D102 - inherited docstring
        self, contact: Contact, text: str
    ) -> Ack | None:
        transmit_gate.mark()
        async with self.transmitting():
            await asyncio.sleep(0)
        # Firmware can only address a contact it holds, so a recipient this simulated device
        # doesn't have is refused exactly as hardware refuses one — which is what makes the
        # chat screen's "add it back and send" offer walkable on the simulator.
        if self._mock_key(contact) not in {self._mock_key(c) for c in self._contacts}:
            raise ContactNotOnDeviceError(contact)
        # Otherwise the simulator "delivers" instantly and always acknowledges, so outbound
        # direct messages show as acked without a radio.
        return Ack(code="mock")

    async def send_channel_message(  # noqa: D102 - inherited docstring
        self, index: int, text: str
    ) -> None:
        transmit_gate.mark()
        async with self.transmitting():
            await asyncio.sleep(0)
            #: What each channel message went out under — the session scope at the moment
            #: it was handed over (``""`` the default, ``"*"`` forced unscoped).
            self.sent_channel.append((index, text, self._send_scope))

    async def admin_login(self, node: Contact, password: str) -> LoginResult:  # noqa: D102
        await asyncio.sleep(0)
        if node.name in self._unreachable:
            return LoginResult.NO_REPLY  # simulates a node that is down or out of range
        if password != self._admin_password:
            return LoginResult.REFUSED
        self._admin_sessions.add(self._mock_key(node))
        return LoginResult.ACCEPTED

    #: The simulated repeater CLI's configuration, keyed and answered the way MeshCore's
    #: ``CommonCLI.cpp`` does (see send_remote_command). What is absent is absent on purpose:
    #: a board with no front-end module and a build with no bridge, so a read of those
    #: answers ``??:`` exactly as that hardware would.
    _REMOTE_CFG_DEFAULTS = {
        "owner.info": "",
        "lat": "0",
        "lon": "0",
        "freq": "910.525",
        "bw": "62.5",
        "sf": "7",
        "cr": "5",
        "radio.rxgain": "on",
        "cad": "off",
        "int.thresh": "0",
        "agc.reset.interval": "0",
        "repeat": "on",
        "txdelay": "0.5",
        "direct.txdelay": "0.2",
        "rxdelay": "0",
        "af": "1",
        "loop.detect": "off",
        "path.hash.mode": "0",
        "multi.acks": "0",
        "flood.max": "64",
        "flood.max.unscoped": "64",
        "flood.max.advert": "8",
        "advert.interval": "240",
        "flood.advert.interval": "12",
        "guest.password": "",
        "allow.read.only": "off",
        "powersaving": "off",
        "adc.multiplier": "0.000",
        "gps advert": "none",
        "bridge.type": "none",
    }

    #: The ``get radio`` fields, in the order the reply joins them.
    _REMOTE_RADIO = ("freq", "bw", "sf", "cr")

    async def send_remote_command(  # noqa: D102 - inherited docstring
        self, node: Contact, command: str, *, timeout: float = 8.0
    ) -> str | None:
        transmit_gate.mark()
        await asyncio.sleep(0)
        key = self._mock_key(node)
        if key not in self._admin_sessions:
            return None  # firmware ignores strangers — reads as a timeout, like hardware
        cfg = self._remote_cfg.setdefault(key, {"name": node.name, **self._REMOTE_CFG_DEFAULTS})
        parts = command.strip().split()
        verb = parts[0].lower() if parts else ""
        if verb == "ver":
            return "MeshCore v1.15.0 (simulator)"
        if verb == "clock":
            return "OK - clock synced" if parts[1:] == ["sync"] else "12:00 - 1/1/2026 UTC"
        if verb == "advert":
            return "OK - Advert sent"
        regions = self._region_maps.get(node.key_prefix or "")
        if verb == "region" and regions is not None:
            return regions.command(command.strip())
        if verb == "reboot" and regions is not None:
            regions.reboot()  # a power cycle drops every region edit not saved
        if verb in ("reboot", "password", "time", "start"):
            return "OK"
        if verb == "neighbors":
            table = self._neighbour_tables.get(node.key_prefix or "", [])
            return "\n".join(f"{p} {snr:+.1f}dB {ago}s" for p, snr, ago in table) or "none"
        if verb == "powersaving":
            if len(parts) == 2 and parts[1] in ("on", "off"):
                cfg["powersaving"] = parts[1]
            return cfg["powersaving"]  # bare, no "> ": a verb, not a get
        if verb == "gps":
            if parts[1:2] == ["advert"]:
                if len(parts) == 2:
                    return f"> {cfg['gps advert']}"
                if parts[2] in ("none", "share", "prefs"):
                    cfg["gps advert"] = parts[2]
                    return "OK"
                return "error: expected none, share or prefs"
            if len(parts) == 1:
                return "off"  # no receiver on this board
            return "gps toggle not supported"
        if verb == "get" and len(parts) == 2:
            param = parts[1].lower()
            if param == "tx":
                return f"> {self._remote_tx.get(key, self._default_remote_tx)}"
            if param == "radio":
                return "> " + ",".join(cfg[name] for name in self._REMOTE_RADIO)
            if param == "dutycycle":
                return f"> {100.0 / (float(cfg['af']) + 1.0):.1f}%"
            if param in cfg and param not in ("gps advert", "powersaving"):
                return f"> {cfg[param]}"
            return f"??: {param}"
        if verb == "set" and len(parts) >= 3:
            param = parts[1].lower()
            value = " ".join(parts[2:])
            if param == "tx":
                self._remote_tx[key] = int(float(value))
                return "OK"
            if param == "radio":
                fields = value.split(",")
                if len(fields) != len(self._REMOTE_RADIO):
                    return "Error, invalid params"
                cfg.update(zip(self._REMOTE_RADIO, fields, strict=True))
                return "OK - reboot to apply"
            if param == "dutycycle":
                duty = float(value)
                if not 1 <= duty <= 100:
                    return "ERROR: dutycycle must be 1-100"
                cfg["af"] = f"{100.0 / duty - 1.0:g}"
                return f"OK - {duty:.1f}%"
            # freq alone is serial-only; the rest of the radio goes through "set radio".
            read_only = (*self._REMOTE_RADIO, "bridge.type", "gps advert", "powersaving")
            if param in cfg and param not in read_only:
                cfg[param] = value
                return "OK"
            return f"unknown config: {param} {value}"
        return f"??: {command.strip()}"

    async def get_remote_tx_power(self, node: Contact) -> int | None:  # noqa: D102
        key = self._mock_key(node)
        if key not in self._admin_sessions:
            return None
        return self._remote_tx.get(key, self._default_remote_tx)

    async def set_remote_tx_power(self, node: Contact, value: int) -> None:  # noqa: D102
        key = self._mock_key(node)
        if key not in self._admin_sessions:
            raise DeviceCommandError(f"not logged in to {node.name!r}; call admin_login first.")
        self._remote_tx[key] = value

    async def fetch_neighbours(self, node: Contact) -> list[NeighbourInfo]:  # noqa: D102
        await asyncio.sleep(0)
        key = self._mock_key(node)
        # Mirrors real firmware (verified on v1.15): without a login the request is
        # silently dropped, which the caller experiences as a timeout.
        if key not in self._admin_sessions:
            raise DeviceCommandError(
                f"{node.name!r} did not answer the neighbour request. Firmware ignores "
                "it without an admin login (log in first)."
            )
        table = self._neighbour_tables.get(key[:8])
        if table is None:
            raise DeviceCommandError(
                f"{node.name!r} did not answer the neighbour request (no neighbour "
                "table on this node type)."
            )
        now = utcnow()
        return [
            NeighbourInfo(node=prefix, snr=snr, heard_at=now - timedelta(seconds=ago))
            for prefix, snr, ago in table
        ]

    async def request_regions(self, node: Contact) -> list[str]:  # noqa: D102
        await asyncio.sleep(0)
        key = self._mock_key(node)
        # Only a node with a region table is a repeater here, and only a repeater answers.
        regions = self._region_maps.get(node.key_prefix or key[:8])
        if node.name in self._unreachable:
            raise DeviceCommandError(
                f"{node.name!r} did not answer the regions request (out of direct reach)."
            )
        if regions is None:
            raise DeviceCommandError(
                f"{node.name!r} did not answer the regions request (not a repeater)."
            )
        return parse_region_list(regions.allowed_names())

    @staticmethod
    def _mock_key(node: Contact) -> str:
        """Return the lookup key for a remote node (its public key, else key prefix)."""
        return (node.public_key or node.key_prefix or node.name).lower().removeprefix("0x")

    def _remote_tx_for(self, hop_hex: str) -> int | None:
        """Resolve the simulated remote TX power set on a forced-path hop, if any.

        The optimizer stores a node's power under its full public key; a trace addresses
        it by a shorter hash prefix, so match in either direction.

        Args:
            hop_hex: The forced-path hop hash (hex).

        Returns:
            The node's simulated TX power, or ``None`` if we never set one (i.e. this
            hop isn't a node the optimizer is tuning).
        """
        h = hop_hex.lower()
        for key, tx in self._remote_tx.items():
            if key.startswith(h) or h.startswith(key):
                return tx
        return None

    async def get_tuning(self) -> dict:  # noqa: D102 - inherited docstring
        return dict(self._tuning)

    async def get_autoadd_config(self) -> int | None:  # noqa: D102
        return self._autoadd_config

    async def get_autoadd_max_hops(self) -> int | None:  # noqa: D102 - inherited docstring
        return self._autoadd_max_hops

    async def get_allowed_repeat_freqs(self) -> list[tuple[float, float]]:  # noqa: D102
        return list(_DEFAULT_REPEAT_FREQS)

    async def get_default_flood_scope(self) -> str | None:  # noqa: D102
        return self._flood_scope

    async def get_time(self) -> int | None:  # noqa: D102 - inherited docstring
        import time as _time

        if self._clock_offset is None:
            return self._info.get("clock")
        return int(_time.time()) + self._clock_offset

    async def get_battery(self) -> dict:  # noqa: D102 - inherited docstring
        return {"level": 4100, "used_kb": 128, "total_kb": 1024}

    async def get_stats(self) -> dict:  # noqa: D102 - inherited docstring
        return {
            "battery_mv": 4100,
            "uptime_secs": 93784,  # 1d 2h 3m 4s
            "errors": 0,
            "queue_len": 0,
            "noise_floor": -110,
            "last_rssi": -62,
            "last_snr": 9.5,
            "tx_air_secs": 42,
            "rx_air_secs": 360,
            "recv": 1234,
            "sent": 210,
            "flood_tx": 40,
            "direct_tx": 170,
            "flood_rx": 900,
            "direct_rx": 334,
            "recv_errors": 3,
        }

    async def get_path_hash_mode(self) -> int:  # noqa: D102 - inherited docstring
        return self._path_hash_mode

    async def get_custom_vars(self) -> dict[str, str]:  # noqa: D102 - inherited docstring
        return dict(self._custom_vars)

    async def get_channel(self, index: int) -> dict | None:  # noqa: D102
        if index >= self._max_channels:
            raise DeviceCommandError(f"channel index {index} out of range")
        return self._channels.get(index)

    async def set_name(self, name: str) -> None:  # noqa: D102 - inherited docstring
        self._info["name"] = name

    async def set_coords(self, lat: float, lon: float) -> None:  # noqa: D102
        self._info["adv_lat"] = lat
        self._info["adv_lon"] = lon

    async def set_device_pin(self, pin: int) -> None:  # noqa: D102 - inherited docstring
        self._device_pin = pin

    async def set_radio(  # noqa: D102 - inherited docstring
        self, freq: float, bw: float, sf: int, cr: int, repeat: bool | None = None
    ) -> None:
        # Modelled on the firmware: relaying is refused off the allowed frequencies, and a
        # command that leaves the repeat byte off turns relaying *off* — the trap a radio
        # change has to restate its way around.
        if repeat and not repeat_freq_allowed(freq, _DEFAULT_REPEAT_FREQS):
            raise DeviceCommandError("device rejected the command: illegal argument")
        self._info.update(radio_freq=freq, radio_bw=bw, radio_sf=sf, radio_cr=cr)
        self._client_repeat = bool(repeat)

    async def set_tuning(self, rx_delay: float, airtime_factor: float) -> None:  # noqa: D102
        # Round-trip through the wire's ×1000 integer scaling so the simulator loses
        # precision exactly where real firmware would.
        self._tuning = {
            "rx_delay": round(float(rx_delay) * 1000) / 1000.0,
            "airtime_factor": round(float(airtime_factor) * 1000) / 1000.0,
        }

    async def set_autoadd_config(  # noqa: D102 - inherited docstring
        self, flags: int, max_hops: int | None = None
    ) -> None:
        self._autoadd_config = int(flags)
        if max_hops is not None:  # the firmware leaves the limit alone when it isn't sent
            self._autoadd_max_hops = min(int(max_hops), 64)

    async def set_default_flood_scope(self, scope: str) -> None:  # noqa: D102
        # Mirror the firmware: empty (or the wildcard) clears, a name is stored bare.
        bare = normalize_region(scope)
        self._flood_scope = "" if bare in ("", REGION_WILDCARD) else validate_region(bare)

    async def set_flood_scope(self, region: str | None) -> None:  # noqa: D102
        bare = normalize_region(region) if region else ""
        self._send_scope = validate_region(bare) if bare and bare != REGION_WILDCARD else bare

    async def set_manual_add_contacts(self, enabled: bool) -> None:  # noqa: D102
        self._info["manual_add_contacts"] = enabled

    async def set_adv_loc_policy(self, policy: int) -> None:  # noqa: D102
        self._info["adv_loc_policy"] = policy

    async def set_multi_acks(self, value: int) -> None:  # noqa: D102 - inherited docstring
        self._info["multi_acks"] = value

    async def set_telemetry_modes(self, base: int, loc: int, env: int) -> None:  # noqa: D102
        self._info.update(telemetry_mode_base=base, telemetry_mode_loc=loc, telemetry_mode_env=env)

    async def set_path_hash_mode(self, mode: int) -> None:  # noqa: D102
        self._path_hash_mode = mode

    async def set_custom_var(self, key: str, value: str) -> None:  # noqa: D102
        self._custom_vars[key] = value

    async def set_channel(self, index: int, name: str, secret: bytes | None) -> None:  # noqa: D102
        self._channels[index] = {
            "channel_idx": index,
            "channel_name": name,
            "channel_secret": secret or (b"\x00" * 16),
        }

    async def set_time(self, epoch: int) -> None:  # noqa: D102 - inherited docstring
        import time as _time

        self._info["clock"] = epoch
        # The simulated clock now runs from the set point (drift corrected).
        self._clock_offset = epoch - int(_time.time())

    async def send_advert(self, flood: bool = False) -> None:  # noqa: D102
        transmit_gate.mark(flood_advert=flood)
        async with self.transmitting():
            await asyncio.sleep(0)

    async def reboot(self) -> None:  # noqa: D102 - inherited docstring
        await asyncio.sleep(0)

    async def export_private_key(self) -> str:  # noqa: D102 - inherited docstring
        return self._private_key

    async def import_private_key(self, key_hex: str) -> None:  # noqa: D102
        self._private_key = bytes.fromhex(key_hex).hex()  # validates hex, normalizes

    async def factory_reset(self) -> None:  # noqa: D102 - inherited docstring
        self._custom_vars.clear()
        self._channels.clear()

    async def subscribe_events(  # noqa: D102 - inherited docstring
        self, on_event: EventCallback
    ) -> Unsubscribe:
        # Simulate a live event stream. Emit a burst of synthetic adverts/telemetry from
        # the known contacts *synchronously* here, then keep emitting at a steady cadence
        # from a background task until unsubscribed. The immediate first burst means even
        # a zero-length window always sees every contact (two of which carry a location)
        # and one inbound message, keeping capture tests deterministic.
        stop = asyncio.Event()
        seq = 0
        burst = 0

        def emit_burst() -> None:
            nonlocal seq, burst
            for contact in self._contacts:
                on_event(MeshEvent.observation_event(self._synth_observation(contact, seq)))
                seq += 1
            # Simulate the companion's RX packet log: overheard packets from the routed
            # leaf nodes, each carrying the relay path it crossed — so topology capture
            # accumulates passive path evidence on the simulator exactly as on hardware
            # with packet logging enabled. The first burst always includes one.
            if burst % 4 == 0:
                on_event(MeshEvent.observation_event(self._synth_packet(burst // 4)))
            # Periodically simulate an inbound direct message so message-driven features
            # (and their tests) have traffic to react to; the first burst always includes
            # one so a subscriber sees a message without waiting.
            if burst % 8 == 0:
                on_event(MeshEvent.message_event(self._synth_message(burst // 8)))
            burst += 1

        emit_burst()

        async def emit_loop() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), _MOCK_MONITOR_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass  # cadence tick elapsed; emit the next burst
                if not stop.is_set():
                    emit_burst()

        task = asyncio.create_task(emit_loop())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

        def unsubscribe() -> None:
            stop.set()  # wakes the loop's wait immediately; it exits on the next check

        return unsubscribe

    #: Fixed locations advertised by the two simulated repeaters, so location-aware
    #: features (e.g. the coverage map) always have coordinates to work with.
    _MOCK_LOCATIONS = {
        "Yagi-Repeater": (45.5019, -73.5674),
        "Local-Repeater": (45.4768, -73.5990),
    }

    def _synth_observation(self, contact: Contact, seq: int) -> Observation:
        """Build one plausible synthetic observation for ``contact`` (simulator only).

        Args:
            contact: The contact to synthesize a reception from.
            seq: Monotonic emission counter, used to vary the packet kind.

        Returns:
            A noisy :class:`Observation` tagged ``telemetry`` on every fourth packet and
            ``advert`` otherwise, carrying a location for the simulated repeaters.
        """
        lat_lon = self._MOCK_LOCATIONS.get(contact.name)
        # The two located nodes are the simulated repeaters (fixed infrastructure); the rest
        # advertise as ordinary chat nodes, so the map has both classes to prioritise.
        node_type = NODE_TYPE_REPEATER if contact.name in self._MOCK_LOCATIONS else NODE_TYPE_CHAT
        return Observation(
            node=contact.key_prefix or contact.public_key[:12],
            public_key=contact.public_key or None,
            name=contact.name,
            kind="telemetry" if seq % 4 == 3 else "advert",
            node_type=node_type,
            snr=round(self._rng.gauss(6.0, 3.0), 1),
            rssi=round(self._rng.gauss(-95.0, 8.0), 1),
            lat=lat_lon[0] if lat_lon else None,
            lon=lat_lon[1] if lat_lon else None,
        )

    def _synth_packet(self, seq: int) -> Observation:
        """Build one plausible RX-logged packet observation (simulator only).

        Rotates over the leaf contacts that sit behind a repeater, emitting the packet
        with the relay path its route implies — matching how a real companion reports an
        overheard relayed frame.

        Args:
            seq: Monotonic emission counter, used to rotate the originating contact.

        Returns:
            A ``packet``-kind :class:`Observation` carrying a one-hop relay path.
        """
        routed = [c for c in self._contacts if c.route_hops]
        contact = routed[seq % len(routed)]
        return Observation(
            node=contact.key_prefix or contact.public_key[:12],
            public_key=contact.public_key or None,
            kind="packet",
            snr=round(self._rng.gauss(6.0, 3.0), 1),
            rssi=round(self._rng.gauss(-95.0, 8.0), 1),
            path=",".join(contact.route_hops or ()),
        )

    def _synth_message(self, seq: int) -> Message:
        """Build one plausible synthetic inbound direct message (simulator only).

        Args:
            seq: Monotonic burst counter, used to rotate the sending contact and body.

        Returns:
            A :class:`Message` from one of the known contacts.
        """
        contact = self._contacts[seq % len(self._contacts)]
        return Message(
            text=f"hello from {contact.name} #{seq}",
            sender=contact.key_prefix or contact.public_key[:12],
            is_channel=False,
            snr=round(self._rng.gauss(6.0, 3.0), 1),
        )

    def _expected_snr(self, hop_index: int) -> float:
        """Model SNR for a hop as an inverted-U in TX power plus distance falloff.

        Args:
            hop_index: Zero-based hop position; deeper hops are weaker.

        Returns:
            The noise-free expected SNR in dB for the current TX power.
        """
        # Inverted parabola peaking at ``optimal_tx``; ~10 dB swing across the range.
        span = (TX_POWER_MAX - TX_POWER_MIN) / 2
        offset = (self._tx_power - self.optimal_tx) / span
        peak = 8.0 - 10.0 * (offset**2)
        return peak - 2.5 * hop_index

    def _expected_remote_snr(self, remote_tx: int) -> float:
        """Model the SNR the target receives from the admin node it sits behind.

        An inverted-U in the admin node's TX power: too low and the target barely hears
        it, too high and the target's front end saturates. The peak sits at
        ``optimal_remote_tx`` so the remote-admin optimizer has a unimodal-with-noise
        curve to converge on.

        Args:
            remote_tx: The admin node's transmit power.

        Returns:
            The noise-free expected SNR in dB at the target.
        """
        # Curvature is steep enough that the band edges fall below the ~-12 dB drop
        # threshold, so traces start failing there — giving the optimizer a real
        # reliability gradient (not just an SNR one) to honor reliability-first.
        span = (REMOTE_TX_MAX - REMOTE_TX_MIN) / 2
        offset = (remote_tx - self.optimal_remote_tx) / span
        return 9.0 - 24.0 * (offset**2)

    async def run_trace(  # noqa: D102 - inherited docstring
        self,
        target: str,
        *,
        path: str | None = None,
        timeout: float | None = None,
    ) -> TraceResult:
        transmit_gate.mark()
        await asyncio.sleep(0.05)  # mimic radio latency so progress bars are visible
        forced = [h for h in path.split(",") if h.strip()] if path else None
        # Mirror the real device: the path-hash width is the byte length of a forced hop.
        hash_bytes = len(bytes.fromhex(forced[0])) if forced else None
        depth = len(forced) if forced else self._rng.randint(1, 3)
        hops: list[Hop] = []
        for i in range(depth):
            # Each hop's SNR reflects the node that transmitted *into* it (hop i-1). If
            # that node is one the optimizer has tuned a remote TX on, model the link from
            # its power — so the hop arriving at the target tracks the admin node we're
            # tuning, wherever the target sits in a there-and-back path. Otherwise fall
            # back to the local TX-power model.
            prev_tx = self._remote_tx_for(forced[i - 1]) if forced is not None and i >= 1 else None
            if prev_tx is not None:
                expected = self._expected_remote_snr(prev_tx)
            else:
                expected = self._expected_snr(i)
            snr = expected + self._rng.gauss(0, 1.2)  # measurement noise
            node = forced[i] if forced else f"hop{i}"
            hops.append(Hop(index=i, node=node, snr=round(snr, 1)))

        # Very weak links occasionally drop the whole trace, judged on the bottleneck hop.
        bottleneck = min((h.snr for h in hops), default=-99)
        success = bottleneck > -12 or self._rng.random() > 0.1

        # The trace reply returns to us: firmware appends the local device as a final
        # hash-less hop (``node=None``). Mirror that so the origin/destination framing
        # naturally has our device at both ends of the path. The return link is modeled
        # as symmetric to the first outbound hop, so it never alters the bottleneck SNR.
        if hops:
            hops.append(Hop(index=depth, node=None, snr=hops[0].snr))
        return TraceResult(
            target=target,
            success=success,
            hops=hops if success else [],
            round_trip_ms=round(self._rng.uniform(120, 480), 1) if success else None,
            tx_power=self._tx_power,
            path_hash_bytes=hash_bytes,
        )


def parse_trace_hops(payload: dict) -> list[Hop]:
    """Extract per-hop SNR from a ``TRACE_DATA`` event payload.

    meshcore parses a trace reply into ``payload["path"]`` — a list of nodes, each a
    dict with a repeater ``"hash"`` and its ``"snr"`` (already in dB, signed-byte / 4).
    The final node is the local device and carries an ``"snr"`` but no ``"hash"``.

    Args:
        payload: The ``TRACE_DATA`` event payload.

    Returns:
        The hops in path order; nodes without an SNR reading are skipped.
    """
    hops: list[Hop] = []
    for node in payload.get("path") or []:
        if not isinstance(node, dict) or node.get("snr") is None:
            continue
        hops.append(Hop(index=len(hops), node=node.get("hash"), snr=float(node["snr"])))
    return hops


def observation_from_event(event, kind: str) -> Observation | None:  # noqa: ANN001
    """Map a meshcore advert/telemetry event into an :class:`Observation`.

    The companion reports a node identifier, optionally a name and shared location, and
    the SNR/RSSI of the reception. Field names vary across firmware and library versions,
    so several common spellings are tried for each value. Like the trace mapping this is
    best-effort and should be validated against your firmware's event schema.

    Args:
        event: A meshcore event (anything exposing a ``payload`` mapping).
        kind: The observation class to tag the record with (e.g. ``advert``).

    Returns:
        The parsed :class:`Observation`, or ``None`` if the payload carried no node id.
    """
    payload = dict(getattr(event, "payload", {}) or {})
    ident = (
        payload.get("public_key")
        or payload.get("pubkey")
        or payload.get("hash")
        or payload.get("key_prefix")
    )
    if not ident:
        return None
    ident = str(ident).lower().removeprefix("0x")
    node = ident[:12]  # the stored 12-hex canonical id (what everything groups/joins on)
    # Keep the whole key when the advert carried one (public_key/pubkey), so a hash lane can
    # later show more than the twelve stored digits; a short-hash-only advert leaves it None.
    public_key = ident if len(ident) > len(node) else None
    lat = payload.get("adv_lat", payload.get("lat"))
    lon = payload.get("adv_lon", payload.get("lon"))
    return Observation(
        node=node,
        public_key=public_key,
        name=payload.get("adv_name") or payload.get("name"),
        kind=kind,
        node_type=_as_int(payload.get("adv_type", payload.get("type"))),
        snr=_as_float(payload.get("snr")),
        rssi=_as_float(payload.get("rssi")),
        lat=_as_float(lat) if lat else None,
        lon=_as_float(lon) if lon else None,
        raw=payload,
    )


def packet_observation_from_event(event) -> Observation | None:  # noqa: ANN001
    """Map a meshcore ``RX_LOG_DATA`` event into a ``packet``-kind :class:`Observation`.

    The companion's RX packet log reports every frame it overhears together with the
    header's relay path — the repeaters the packet crossed before reaching us, nearest
    the originator first. That path is the passive topology evidence the trace path
    composer runs on, so it is preserved verbatim (as comma-separated per-hop hex).

    The originating node is only knowable when the payload class reveals it: the library
    decodes adverts inline (``adv_key``/``adv_name``), so those carry an origin; other
    packet classes are recorded origin-less — their path (plus our reception of its last
    relay) is still adjacency evidence.

    A frame that crossed no relays at all is kept too, with an empty path. Far from
    teaching us nothing, it is the strongest adjacency evidence there is — we heard the
    transmitter directly — and it is the whole of what an adjacent device puts on the air,
    so discarding it made a neighbour's traffic invisible. Only a frame the library could
    not even assign a payload class to is dropped.

    One class needs its header read differently from the rest: a ``TRACE``'s ``path``
    field carries per-hop SNR readings rather than relay hashes (see
    :func:`~meshterm.core.frames.trace_link_snrs`), so it yields no hops and its readings
    are merged in as ``trace_snrs`` instead.

    Origin-less does not mean featureless, though: what the frame *addresses* is decoded
    out of its undecoded body (:func:`~meshterm.core.frames.frame_addressing`) and merged
    into the raw payload — the recipient and sender hashes of a direct message or request,
    an anonymous request's whole sender key, a channel datagram's envelope, an ack's
    checksum, a trace's tag — so every class has something to say about itself downstream.

    Args:
        event: A meshcore ``RX_LOG_DATA`` event (anything exposing a ``payload`` mapping).

    Returns:
        The parsed :class:`Observation` (``kind="packet"``), or ``None`` for a frame with
        no decodable payload class or an unparsable path.
    """
    payload = dict(getattr(event, "payload", {}) or {})
    typename = payload.get("payload_typename")
    if not typename or typename == "UNK":
        return None  # the library's sentinel for a frame too short to have a class at all
    path_len = _as_int(payload.get("path_len")) or 0
    hash_size = _as_int(payload.get("path_hash_size")) or 1
    path_hex = str(payload.get("path") or "").lower().removeprefix("0x")
    hops: list[str] = []
    # A trace's path field is SNR readings, not relay hashes (see
    # :func:`~meshterm.core.frames.trace_link_snrs`), so it contributes no hops — its
    # readings are recovered below and kept beside the frame instead.
    if path_len > 0 and typename != "TRACE":
        width = hash_size * 2
        hops = [path_hex[i * width : (i + 1) * width] for i in range(path_len)]
        if any(len(h) != width for h in hops):
            return None  # a truncated path would fabricate adjacency between wrong nodes

    origin = payload.get("adv_key")
    # A frame with no origin and no relays is not featureless — it is the strongest
    # adjacency evidence the mesh produces: we heard the transmitter *directly*, with
    # nothing in between. Dropping it made every non-advert frame from an adjacent node
    # invisible, which is why a companion device sitting beside this one could trace all
    # day and never appear in the feed. What it carries — its class, what it addresses,
    # how well it was heard — is recorded exactly as a relayed frame's is, with an empty
    # path standing for the zero hops it crossed.
    #
    # What the frame addresses — the recipient, the sender, the channel, the token it
    # carries — read out of the body the library leaves undecoded for every class but
    # advert and channel text (see :mod:`~meshterm.core.frames`). Merged in under its own
    # keys so a class that names no origin node still says what it is *about*.
    payload.update(frame_addressing(payload))
    readings = trace_link_snrs(payload)
    if readings is not None:
        # Kept beside the frame, not in ``path``: a trace's hop readings say how well each
        # leg was heard, and nothing at all about who relayed it.
        payload["trace_snrs"] = readings
    ident = str(origin).lower().removeprefix("0x") if origin else None
    node = ident[:12] if ident else None
    public_key = ident if ident and len(ident) > 12 else None  # keep the whole adv_key
    lat = payload.get("adv_lat")
    lon = payload.get("adv_lon")
    return Observation(
        node=node,
        public_key=public_key,
        name=payload.get("adv_name"),
        kind="packet",
        node_type=_as_int(payload.get("adv_type")),
        snr=_as_float(payload.get("snr")),
        rssi=_as_float(payload.get("rssi")),
        lat=_as_float(lat) if lat else None,
        lon=_as_float(lon) if lon else None,
        path=",".join(hops),
        raw=payload,
    )


def message_from_event(event) -> Message | None:  # noqa: ANN001
    """Map a meshcore ``CONTACT_MSG_RECV`` / ``CHANNEL_MSG_RECV`` event into a message.

    Direct messages carry a ``pubkey_prefix`` sender; channel messages carry a
    ``channel_idx`` instead (``type`` is ``"PRIV"`` or ``"CHAN"``). Field names are
    best-effort and should be validated against your firmware's event schema.

    Args:
        event: A meshcore message event (anything exposing a ``payload`` mapping).

    Returns:
        The parsed :class:`Message`, or ``None`` if the payload carried no text body.
    """
    payload = dict(getattr(event, "payload", {}) or {})
    text = payload.get("text")
    if text is None:
        return None
    is_channel = payload.get("type") == "CHAN" or "channel_idx" in payload
    ts = payload.get("sender_timestamp")
    sender_ts = (
        datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float)) and ts else None
    )
    return Message(
        text=str(text),
        sender=None if is_channel else payload.get("pubkey_prefix"),
        channel=payload.get("channel_idx") if is_channel else None,
        is_channel=is_channel,
        sender_timestamp=sender_ts,
        snr=_as_float(payload.get("SNR", payload.get("snr"))),
        raw=payload,
    )


def ack_from_event(event) -> Ack:  # noqa: ANN001
    """Map a meshcore ``ACK`` event into an :class:`Ack` (delivery acknowledgement)."""
    payload = dict(getattr(event, "payload", {}) or {})
    return Ack(code=payload.get("code"), raw=payload)


def _as_float(value: object) -> float | None:
    """Best-effort float conversion, returning ``None`` on missing/garbage values."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    """Best-effort int conversion, returning ``None`` on missing/garbage values."""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _contact_route(info: dict) -> tuple[str, ...] | None:
    """Extract a contact's device-learned outbound route as per-hop hex hashes.

    The firmware distills the paths of received flood packets into each contact's
    ``out_path``: the repeater chain to send through, one path-hash per hop, from us
    outward. The wire reports the hop count and hash width packed into one byte
    (``0xFF`` = no learned route, i.e. flood) and the path itself as a fixed 64-byte
    field, so the real route is the leading ``out_path_len × size`` bytes.

    Args:
        info: One contact's raw info mapping from the companion's contacts payload.

    Returns:
        The route as a tuple of per-hop hex hashes (empty = a learned *direct* route),
        or ``None`` when no route is learned or the report is unparsable.
    """
    out_path_len = _as_int(info.get("out_path_len"))
    if out_path_len is None or out_path_len < 0:
        return None  # 0xFF on the wire: flood routing, no learned path
    if out_path_len == 0:
        return ()
    mode = _as_int(info.get("out_path_hash_mode"))
    size = max((mode if mode is not None and mode >= 0 else 0) + 1, 1)
    out_path = str(info.get("out_path") or "").lower().removeprefix("0x")
    try:
        route = bytes.fromhex(out_path)[: out_path_len * size]
    except ValueError:
        return None
    hops = tuple(route[i * size : (i + 1) * size].hex() for i in range(out_path_len))
    if any(len(h) != size * 2 for h in hops):
        return None  # the field was shorter than the declared route; don't guess
    return hops


def _contact_location(info: dict) -> tuple[float | None, float | None]:
    """Extract a contact's advertised ``(lat, lon)``, or ``(None, None)`` if it has none.

    A node that has never set coordinates advertises ``0.0/0.0`` (null island), which the
    firmware reports verbatim; we treat that as "no location" rather than plotting the
    Gulf of Guinea.

    Args:
        info: One contact's raw info mapping from the companion's contacts payload.

    Returns:
        The advertised latitude and longitude in decimal degrees, or ``(None, None)``.
    """
    lat = _as_float(info.get("adv_lat", info.get("lat")))
    lon = _as_float(info.get("adv_lon", info.get("lon")))
    if lat is None or lon is None or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
        return None, None
    return lat, lon


def _mock_pub(prefix: str) -> str:
    """Build a 32-byte mock public key from a short hex prefix (simulator only).

    Args:
        prefix: Leading hex digits identifying the node.

    Returns:
        A 64-hex-character (32-byte) key beginning with ``prefix``.
    """
    return prefix + "0" * (64 - len(prefix))


def _parse_tx_reply(reply: str | None) -> int | None:
    """Extract a TX-power integer from a repeater's ``get tx`` reply text.

    Repeater firmware answers tersely and inconsistently across versions (e.g.
    ``"tx: 20"``, ``"TX power = 20 dBm"``, or just ``"20"``), so pull the first signed
    integer out of the reply rather than matching a fixed format.

    Args:
        reply: The node's reply text, or ``None`` if it did not answer.

    Returns:
        The parsed TX power, or ``None`` if the reply was empty or carried no number.
    """
    if not reply:
        return None
    import re

    match = re.search(r"-?\d+", reply)
    return int(match.group()) if match else None


def clamp_tx_power(value: int) -> int:
    """Clamp a TX power value to the supported range.

    Args:
        value: Requested TX power level.

    Returns:
        ``value`` constrained to ``[TX_POWER_MIN, TX_POWER_MAX]``.
    """
    return max(TX_POWER_MIN, min(TX_POWER_MAX, value))


def make_device(
    *,
    mock: bool,
    port: str | None,
    baudrate: int = 115200,
    mock_optimal_tx: int = 14,
    transport: str = "serial",
    address: str | None = None,
    pin: str | None = None,
    ble_device: object | None = None,
    host: str | None = None,
    tcp_port: int | None = None,
    spi: SpiWiring | None = None,
    state: Path | None = None,
) -> Device:
    """Construct the appropriate :class:`Device` for the current invocation.

    Args:
        mock: When ``True`` return a :class:`MockDevice` simulator.
        port: Serial port for a real serial device. Required for the serial transport
            unless ``mock`` is set.
        baudrate: Serial baud rate for a real serial device.
        mock_optimal_tx: Peak TX power for the simulator.
        transport: ``"serial"`` (default), ``"ble"``, ``"tcp"``, or ``"spi"``.
        address: Bluetooth address for the BLE transport. Required when ``transport="ble"``.
        pin: Optional BLE pairing PIN (BLE only).
        ble_device: The scanned ``bleak.BLEDevice`` for ``address``, when this session's
            discovery produced one (BLE only) — lets the connect skip re-discovering the
            peripheral by address (see :class:`MeshCoreDevice`).
        host: Hostname/IP for the TCP transport. Required when ``transport="tcp"``.
        tcp_port: TCP port for the TCP transport. Required when ``transport="tcp"``.
        spi: How the radio is wired, for the SPI transport. Required when ``transport="spi"``.
        state: The SPI radio's state directory (see :func:`meshterm.core.spiradio.state_dir`).
            Required when ``transport="spi"``.

    Returns:
        A connected-on-enter :class:`Device` instance.

    Raises:
        ValueError: If a real device is requested without a usable endpoint for its transport.
    """
    if mock:
        return MockDevice(optimal_tx=mock_optimal_tx)
    if transport == "ble":
        if not address:
            raise ValueError(
                "No Bluetooth address configured. Pass --ble, pick a device at startup, "
                "or use --mock."
            )
        return MeshCoreDevice(transport="ble", address=address, pin=pin, ble_device=ble_device)
    if transport == "tcp":
        if not host or not tcp_port:
            raise ValueError(
                "No network address configured. Pass --tcp host:port, set a TCP profile, "
                "pick a device at startup, or use --mock."
            )
        return MeshCoreDevice(transport="tcp", host=host, tcp_port=tcp_port)
    if transport == "spi":
        if spi is None or state is None:
            raise ValueError(
                "No SPI radio configured. Pass --spi, set an SPI profile, or use --mock."
            )
        from .spiradio import SpiRadioDevice  # imports this module, so not at the top

        return SpiRadioDevice(spi, state)
    if not port:
        raise ValueError("No serial port configured. Pass --port, set a profile, or use --mock.")
    return MeshCoreDevice(port=port, baudrate=baudrate)


#: Handshake window (seconds) for probing a serial companion. A genuine board answers in well
#: under a second; a non-MeshCore port is rejected within this bound.
_PROBE_TIMEOUT_SERIAL_S = 6.0

#: Handshake window (seconds) for probing a BLE companion. Longer than serial: a BLE connect
#: involves a link-layer connection and GATT service discovery before the identity reply.
_PROBE_TIMEOUT_BLE_S = 20.0

#: Handshake window (seconds) for probing a TCP companion. Between serial and BLE: a TCP
#: connect is a quick socket open, but an unreachable host can sit in the OS connect backoff,
#: so the window allows for that before the endpoint is written off as absent.
_PROBE_TIMEOUT_TCP_S = 10.0


async def probe_device(
    device: DiscoveredDevice,
    *,
    baudrate: int = 115200,
    pin: str | None = None,
    spi_state: Path | None = None,
) -> tuple[MeshCoreDevice, dict] | None:
    """Open a discovered device, confirm a MeshCore companion answers, and keep it connected.

    Transport-agnostic front door for the startup smoke test: it opens the right connection
    for ``device`` (serial port or BLE address) and issues an identity query (the APPSTART
    that backs :meth:`MeshCoreDevice.get_self_info`). A genuine companion replies with a
    self-info payload; anything else — a non-MeshCore gadget, an unresponsive port, a BLE
    device out of range — never answers and is rejected within the transport's timeout.

    On success the connection is **left open** and returned to the caller, which reuses it as
    the session device. This is deliberate: many companion boards reset on each serial open
    (and a BLE reconnect re-runs service discovery), so a probe-then-reopen cycle is slow and
    flaky — opening the radio exactly once is both faster and far more reliable. On any
    failure the probe connection is closed.

    Args:
        device: The discovered device to probe (serial or BLE).
        baudrate: Serial baud rate (serial transport only).
        pin: Optional BLE pairing PIN (BLE transport only).
        spi_state: The radio's state directory (SPI transport only) — probing an SPI radio
            starts its node, and the node needs somewhere to keep its identity.

    Returns:
        ``(device, self_info)`` with a connected :class:`MeshCoreDevice` on success (the
        caller owns and must eventually close it), or ``None`` if it is not a reachable
        MeshCore companion.

    Raises:
        DeviceCommandError: On an actionable failure the user can fix — e.g. a Bluetooth
            companion that requires a pairing PIN — so the caller can show the remedy rather
            than an unhelpful "didn't answer".
    """
    if device.is_ble:
        timeout = _PROBE_TIMEOUT_BLE_S
        probe = MeshCoreDevice(
            transport="ble",
            address=device.address,
            pin=pin,
            connect_timeout=timeout,
            # The scan that discovered the device already holds its BLEDevice; connecting
            # through it skips the by-address re-discovery that makes BLE startups flaky.
            ble_device=device.ble_device,
        )
    elif device.is_tcp:
        timeout = _PROBE_TIMEOUT_TCP_S
        probe = MeshCoreDevice(
            transport="tcp",
            host=device.host,
            tcp_port=device.tcp_port,
            connect_timeout=timeout,
        )
    elif device.is_spi:
        from .config import SpiWiring
        from .spiradio import READY_TIMEOUT_S, SpiRadioDevice

        if spi_state is None:
            raise ValueError("probing an SPI radio needs its state directory")
        # The window covers starting the node, not just the handshake: the radio library's
        # import and the chip's bring-up come first, and a board this small is slow at both.
        timeout = READY_TIMEOUT_S
        probe = SpiRadioDevice(device.spi or SpiWiring(), spi_state)  # type: ignore[arg-type]
    else:
        timeout = _PROBE_TIMEOUT_SERIAL_S
        probe = MeshCoreDevice(port=device.port, baudrate=baudrate, connect_timeout=timeout)
    return await _probe(probe, timeout)


async def probe_meshcore(
    port: str, baudrate: int = 115200, *, timeout: float = _PROBE_TIMEOUT_SERIAL_S
) -> tuple[MeshCoreDevice, dict] | None:
    """Probe a serial ``port`` for a MeshCore companion (see :func:`probe_device`).

    Thin serial-only convenience wrapper retained for callers that hold a bare port string.

    Args:
        port: Serial port to probe (e.g. ``COM5`` or ``/dev/ttyUSB0``).
        baudrate: Serial baud rate.
        timeout: Seconds to bound the connection handshake and the identity reply.

    Returns:
        ``(device, self_info)`` on success, or ``None`` if the port is not a reachable
        MeshCore companion.
    """
    return await _probe(
        MeshCoreDevice(port=port, baudrate=baudrate, connect_timeout=timeout), timeout
    )


async def _probe(device: MeshCoreDevice, timeout: float) -> tuple[MeshCoreDevice, dict] | None:
    """Connect ``device`` and read its identity, returning it live or closing it on failure.

    Args:
        device: An unconnected :class:`MeshCoreDevice` configured for its transport.
        timeout: Handshake window bounding both the connect and the identity read.

    Returns:
        ``(device, self_info)`` with the connection left open, or ``None`` if the endpoint
        simply isn't a reachable MeshCore companion.

    Raises:
        DeviceCommandError: On an *actionable* failure the user can fix — e.g. a Bluetooth
            companion that needs a pairing PIN. This is deliberately distinct from ``None``
            (an unremarkable "not a companion" miss) so the caller can show the real remedy
            instead of a generic "didn't answer".
    """
    # ``timeout`` is the handshake window handed to the client, so a non-MeshCore endpoint is
    # rejected in ~``timeout`` seconds and the client cleans up its own connection. The outer
    # ``wait_for`` is only a safety net a few seconds beyond that, so we never cancel the
    # client mid-handshake (which would leak the open connection). A real companion answers in
    # well under a second (serial) or a few seconds (BLE), so this never delays a good device.
    stage = "connect"
    try:
        await asyncio.wait_for(device.connect(), timeout + 4.0)
        stage = "identity"
        info = await asyncio.wait_for(device.get_self_info(), timeout)
    except asyncio.TimeoutError as exc:
        await _safe_disconnect(device)
        if getattr(device, "transport", None) != "ble":
            return None
        # A Bluetooth connect has more stages than a serial one (link, pairing, service
        # discovery, the identity reply), so saying which one ran out of time is what
        # tells a device out of range apart from one that connected and then went quiet.
        where = device.endpoint or "the selected Bluetooth device"
        if stage == "connect":
            message = (
                f"connecting to {where} over Bluetooth took longer than {timeout + 4.0:.0f}s — "
                "the link, pairing, or service discovery stalled. Move closer, restart the "
                "device, and try again."
            )
        else:
            message = (
                f"connected to {where} over Bluetooth, but it didn't answer the identity query "
                f"within {timeout:.0f}s — it may be busy with another app; restart it and try "
                "again."
            )
        _log.warning("BLE probe of %s timed out at the %s stage", where, stage)
        raise DeviceCommandError(message) from exc
    except DeviceCommandError:
        # A clean, actionable failure (the device needs a PIN, say): close the probe and let it
        # through so the picker surfaces the remedy rather than hiding it behind "didn't answer".
        await _safe_disconnect(device)
        raise
    except Exception:  # noqa: BLE001 - any other failure just means "not confirmed"
        await _safe_disconnect(device)
        return None
    if not info:
        await _safe_disconnect(device)
        return None
    # The connection is already open, so learn the hardware model now (its own protocol frame)
    # and fold it into the identity dict — this is the one moment the model is obtainable, and
    # it lets the caller remember "Seeed Tracker T1000-E" without a second connect. It's purely
    # additive: self-info fields win on the (currently non-overlapping) keys, and a firmware
    # that can't answer the query simply contributes nothing.
    try:
        device_info = await asyncio.wait_for(device.get_device_info(), timeout)
    except Exception:  # noqa: BLE001 - model is a nicety; never fail a good probe over it
        device_info = {}
    return device, {**device_info, **info}


async def _safe_disconnect(device: Device) -> None:
    """Best-effort disconnect that never raises (used to discard a failed probe)."""
    try:
        await device.disconnect()
    except Exception:  # noqa: BLE001 - best-effort cleanup of the probe connection
        pass
