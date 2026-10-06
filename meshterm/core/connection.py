# SPDX-License-Identifier: Apache-2.0
"""The abstraction of the connection to the device.

Services and tools use only the :class:`Device` interface. They never use the
``meshcore`` library directly. Thus the algorithms are easy to test, and the
:class:`MockDevice` simulator can replace real hardware during development.

There are two implementations:

* :class:`MeshCoreDevice`: a wrapper around the async ``meshcore`` client for the
  companion protocol.
* :class:`MockDevice`: a deterministic simulator. Its SNR changes with the TX power in a
  physically plausible way. ``--mock`` and the test suite use it.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import random
import re
import socket
import sys
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import transmit_gate
from .channels import CHANNEL_SLOT_PROBE_CAP
from .events import MeshEvent
from .frames import frame_addressing, trace_link_snrs
from .models import (
    NODE_TYPE_CHAT,
    NODE_TYPE_REPEATER,
    NODE_TYPE_ROOM,
    Ack,
    Contact,
    Delivery,
    Hop,
    LoginResult,
    Message,
    NeighbourInfo,
    Observation,
    RoomAccess,
    RoomLogin,
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

#: The module logger. To trace the message pump, set DEBUG on ``meshterm.core.connection``.
_log = logging.getLogger(__name__)

#: A callback that receives each :class:`~meshterm.core.events.MeshEvent` from the device
#: (an overheard packet, a received message, an acknowledgement).
EventCallback = Callable[[MeshEvent], None]

#: A callable with no arguments that :meth:`Device.subscribe_events` returns. A call to it
#: stops the subscription and releases its resources.
Unsubscribe = Callable[[], None]

#: The interval at which the :class:`MockDevice` simulator makes a new burst of synthetic
#: packets while a passive-monitor subscription is open (seconds).
_MOCK_MONITOR_INTERVAL_S = 0.05

#: The interval at which the message pump of the real device looks for queued messages
#: (seconds). This is a safety net for firmware that does not always push
#: ``MESSAGES_WAITING``.
_MESSAGE_POLL_INTERVAL_S = 3.0

#: The standard Bluetooth GATT Battery Service and its Battery Level Status characteristic
#: (GATT Specification Supplement, "Battery Level Status", 0x2BED, added in Battery Service
#: 1.1). When a companion has them, the status characteristic has a charging flag that the
#: firmware reports. This flag is a direct fact, and the MeshCore companion protocol itself
#: never gives it (the protocol reports only a battery voltage). MeshCore firmware does not
#: have this service now: its BLE profile is only the Nordic UART pipe and DFU. A dump of the
#: GATT table confirmed this. Thus this path is not in use now. It has a fail-safe fallback,
#: and it starts to operate automatically only if a future device has the service. Refer to
#: :func:`charging_from_battery_level_status`.
_BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
_BATTERY_LEVEL_STATUS_UUID = "00002bed-0000-1000-8000-00805f9b34fb"


def charging_from_battery_level_status(data: bytes) -> bool | None:
    """Decode the charging state from a GATT *Battery Level Status* value (0x2BED).

    The Bluetooth GATT Specification Supplement gives this layout. The characteristic starts
    with a 1-byte flags field. A 16-bit little-endian *Power State* word follows it. The two
    bits at offset 5 of this word are the **Charge State** enum: 0 unknown, 1 charging, 2
    discharging (active), 3 discharging (inactive). The fields after the word (identifier,
    battery level, more status) are optional, and this function does not read them. Thus
    only the first three bytes are necessary.

    Args:
        data: The raw characteristic value, as read over GATT.

    Returns:
        ``True`` when the battery pack reports that it charges, ``False`` when it reports
        that it discharges, or ``None`` when the value is too short or the state is unknown.
        In the last case, the caller uses the inference from the voltage trend instead.
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


#: The timeout for each ``get_msg`` in the message pump (seconds). Thus a device reply that
#: does not come cannot block the drain loop.
_MESSAGE_GET_TIMEOUT_S = 5.0

#: MeshCore's ``TXT_TYPE_SIGNED_PLAIN``: the text type that a room server uses to push its
#: posts. The body starts with the first four bytes of the author's key. (``0`` is plain
#: text, and ``1`` is a command-line exchange.)
_TXT_TYPE_SIGNED_PLAIN = 2


@dataclass(frozen=True, slots=True)
class _LoginExchange:
    """One login exchange, as :meth:`MeshCoreDevice._login` saw it.

    Attributes:
        result: How the exchange ended.
        payload: The payload of the ``LOGIN_SUCCESS`` reply that accepted the login. It is
            ``None`` if the login was not accepted.
        flood: How the radio confirmed that it sent the login: ``True`` by flood, ``False``
            along its learned route. It is ``None`` when the radio never confirmed that it
            sent the login.
        radio_error: The reason why the companion did not send the login, when it never
            sent it and said why. It is ``None`` in other cases.
    """

    result: LoginResult
    payload: dict | None
    flood: bool | None
    radio_error: str | None = None


#: The maximum number of uncorrelated frames that one :meth:`MeshCoreDevice.admin_login`
#: can quote in its log line. These frames help a person who examines a failure later. They
#: are not records to keep. A busy mesh can push very many of them during the window. A few
#: frames are sufficient to identify the fault, and more frames only hide the result that
#: is on the same line.
_LOGIN_STRAY_LOG_CAP = 4

#: The maximum time for a graceful teardown of the ``meshcore`` client (seconds). After this
#: time, MeshTerm stops the teardown and force-closes the transport instead. A healthy
#: disconnect takes much less than one second. The limit is necessary because the shutdown
#: of the dispatcher of the library can deadlock. Its ``queue.join()`` never returns when two
#: or more events (a usual serial RX burst) are in the queue at the time of the stop, because
#: the processor task exits after it drains only one event. The value is well below the
#: 5-second exit watchdog of the interactive session. Thus the forced path also ends as a
#: clean exit, not as an ``os._exit`` reap.
_DISCONNECT_TIMEOUT_S = 2.0

#: The time that :meth:`MeshCoreDevice.reboot` waits for the reboot write before it
#: considers the write as sent (seconds). Over Bluetooth, each command is a
#: write-with-response. A board that restarts on the command may never send the link-layer
#: acknowledgement. Then the write hangs until the link supervision timeout ends it. This
#: takes one second or more, and during this time the app seems to ignore the key press. A
#: write that fails does so well within this time.
_REBOOT_WRITE_GRACE_S = 0.25

#: The reboot writes that still wait for an acknowledgement, which probably never comes.
#: This set keeps a reference to each write for the loop, until the teardown after the
#: reboot ends it.
_REBOOT_WRITES: set[asyncio.Future] = set()


def _forget_reboot_write(write: asyncio.Future) -> None:
    """Remove a finished reboot write, and read its result so no error is logged as unhandled."""
    _REBOOT_WRITES.discard(write)
    if not write.cancelled() and write.exception() is not None:
        _log.debug("reboot write ended after the device went away: %s", write.exception())


#: The time limit for the forced close of the transport after MeshTerm stops a graceful
#: teardown (seconds). ``_DISCONNECT_TIMEOUT_S + _FORCE_DISCONNECT_TIMEOUT_S`` stays below
#: the exit watchdog.
_FORCE_DISCONNECT_TIMEOUT_S = 1.5

#: The time limit for the graceful part of the close of a meshcore client whose own
#: ``connect`` failed (seconds). It is short on purpose. This close runs on a failure path
#: while the user waits: the startup probe that will raise "needs a PIN". Also, a half-open
#: client has no session state that is worth a drain. The forced close of the transport
#: always follows.
_DISCARD_TIMEOUT_S = 2.0

#: The total number of tries to open the BLE link before MeshTerm reports the failure. On
#: Windows, the open of a BLE connection sometimes fails for no clear reason. For example,
#: the internal lookup of bleak does not find a peripheral that advertises slowly, or the
#: link-layer connect races the discovery scan that just finished. One retry recovers the
#: usual case, and it adds only a short delay for a device that is not there. A link that
#: opens and then is lost during service discovery gets a retry in the same way (the radio
#: of a Cardputer Zero does this sometimes). This retry is only for the open of the link.
#: It is never for a rejected PIN, and never for a mesh transmission.
_BLE_CONNECT_ATTEMPTS = 2

#: The pause between two tries to open the BLE link (seconds). It gives the OS radio a short
#: time to become stable.
_BLE_CONNECT_RETRY_DELAY_S = 1.0

#: The number of tries, and the wait between them, while macOS completes a pairing that it
#: has just started. On macOS, a subscribe without a bond to the authenticated
#: characteristic of the companion starts Passkey Entry. Thus the failure that we catch is
#: the signal that the OS has opened its dialog. The user must now read a 6-digit code on
#: the device and type it. When MeshTerm stopped at once, a correct PIN gave an error, and
#: only the second try was successful, because the first try had silently made the bond.
#: The values give a person sufficient time to type a code (they are not for a radio that
#: becomes stable). They have a limit, so a cancelled dialog still fails at the end.
_BLE_MACOS_PAIRING_ATTEMPTS = 5
_BLE_MACOS_PAIRING_DELAY_S = 6.0

TX_POWER_MIN = 1
TX_POWER_MAX = 22

#: The default range that MeshTerm explores when it tunes the transmit power of a remote
#: repeater. Remote nodes (for example, high-gain repeaters) usually transmit at a higher
#: power than our companion. Thus this range is different from the ``TX_POWER_MIN``/
#: ``TX_POWER_MAX`` clamp of our companion. The user can change both limits (refer to
#: :class:`~meshterm.core.config.Settings`).
REMOTE_TX_MIN = 12
REMOTE_TX_MAX = 28


class DeviceCommandError(RuntimeError):
    """A device command failed in a way that MeshTerm can recover from and show to the user.

    MeshTerm raises it for conditions that it must report cleanly (with no traceback). The
    main condition is that the companion sometimes does not answer a query in time. Callers
    can try again.
    """


class ContactNotOnDeviceError(DeviceCommandError):
    """The companion has no contact that matches the recipient, so it cannot address it.

    To address a direct message, the firmware looks for the recipient in its own contact
    table (by a key prefix). When nothing matches, it answers
    ``ERR_CODE_NOT_FOUND``. This is the only send rejection with an obvious repair: put the
    contact back on the device. This error is a separate :class:`DeviceCommandError`
    subclass, so the chat screen can offer that repair (refer to
    :func:`~meshterm.ui.chat.open_chat`). All other callers continue to treat it as a usual
    command failure.

    This error can occur for a contact that MeshTerm itself showed in a list. The reason is
    that the contact list on a screen is the union of the live table of the device and the
    contacts that MeshTerm remembers for it (refer to :mod:`meshterm.core.contact_store`).
    A contact that the firmware removed after that time is still in the list, and only the
    send finds that it is not there.

    :meth:`Device.remove_contact` raises it for the same lookup in the same table. There it
    has the opposite meaning: there is nothing more to delete on the radio. Thus a removal
    reports this error and continues to remove the contact from what MeshTerm remembers. It
    does not fail, because a failure would leave a row that the user cannot remove.

    Attributes:
        contact: The recipient that the device could not find.
    """

    def __init__(self, contact: Contact) -> None:
        """Explain the rejection with the name of the contact that the device could not find.

        Args:
            contact: The recipient for which the companion has no entry.
        """
        super().__init__(f"{contact.name} isn't in this device's contacts — add it back to send.")
        self.contact = contact


class ClockAheadError(DeviceCommandError):
    """The radio clock is ahead of the written time, and the firmware does not set it back.

    MeshCore's companion firmware sets its clock only forward. If ``CMD_SET_DEVICE_TIME``
    has a time earlier than the time of the clock, the answer is ``ERR_CODE_ILLEGAL_ARG``
    (``examples/companion_radio/MyMesh.cpp``). The error table translates this answer as
    "malformed". That is true of the argument, but it does not help anyone. A GPS fix,
    another app, or usual drift can move the clock past the clock of this computer. The
    clock then stays there until the radio reboots. Thus when a set meets such a clock, it
    is not a failure to try again. It is a fact to state.

    Attributes:
        ahead_s: How far ahead the radio clock was, in seconds. It is ``None`` when MeshTerm
            did not read it (the refusal of the firmware says only that the clock is ahead).
    """

    def __init__(self, ahead_s: int | None) -> None:
        """Tell how far ahead the clock is, and what moves it and what does not.

        Args:
            ahead_s: The seconds ahead of the written time, or ``None`` when not known.
        """
        by = f" {_span(ahead_s)}" if ahead_s is not None else ""
        super().__init__(
            f"the radio's clock is{by} ahead of this computer's, and MeshCore firmware never "
            "sets its clock back — rebooting the radio resets it"
        )
        self.ahead_s = ahead_s


def _span(seconds: int) -> str:
    """A duration in the form that a person says it: ``3 s``, ``12 min``, ``5 h``, ``2 days``."""
    seconds = abs(int(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 2 * 3600:
        return f"{seconds // 60} min"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


class DeviceAuthenticationError(DeviceCommandError):
    """A Bluetooth companion refused the connection, because a pairing PIN or a bond is necessary.

    This error is a separate :class:`DeviceCommandError` subclass. Thus callers can tell
    "this device needs a PIN" from a usual command failure, and they can offer to get a PIN.
    The interactive picker opens a PIN dialog and tries again. The scripted CLI (which
    catches the base class) prints the message and stops, because it cannot prompt. The
    message already gives the repair (``--ble-pin`` and OS pairing).

    This one exception is for several different failures: no PIN, an old bond that the OS
    still trusts, a wrong PIN, or a pairing that the device itself refused. Each failure has
    its own repair. Thus the message tells which failure occurred (refer to
    :meth:`MeshCoreDevice._ble_auth_failure`), instead of one sentence for all of them.

    Attributes:
        hint: The same diagnosis, made shorter to one line for the PIN dialog. The dialog
            asks again below this line. It is empty when there is nothing to say in addition
            to the question of the dialog.
    """

    def __init__(self, message: str, *, hint: str = "") -> None:
        """Make the error.

        Args:
            message: The full sentence that the user can act on: what failed and what to do.
            hint: The version of the message that fits in the dialog (refer to the class
                attributes).
        """
        super().__init__(message)
        self.hint = hint


class UnrecognisedConnectError(DeviceCommandError):
    """A connect failed in a way for which MeshTerm has no name.

    Each failure that MeshTerm recognizes has its own sentence, which tells what occurred
    and what to do. This error is for a failure that MeshTerm does not recognize. MeshTerm
    does not let a bare library error through. A dialog could only call such an error
    "didn't answer", and in the past it left nothing in the log. Instead, this error tells
    what MeshTerm tried to open, at which step, and the words of the original error.
    MeshTerm also logs the full traceback with it (refer to :func:`_unrecognised`), so a
    dialog can tell where to find the traceback. MeshTerm raises this error instead of the
    original error, which stays its ``__cause__``.
    """


class FloodScopeError(DeviceCommandError):
    """The companion could not send in the scope that a channel send asked for, so it did not send.

    A scoped send has three steps: set the scope, send, and restore (refer to
    :meth:`Device.send_channel_in_scope`). When the first step fails, MeshTerm does not
    send the message at all. An unscoped send would reach each repeater that the user
    wanted to keep the message from. A send in the default scope of the device would reach
    a region that the user never selected. Each of the two is a silent replacement for the
    request. The message gives the firmware that is necessary for each type of scope,
    because an old companion is the most probable reason by far.

    Attributes:
        scope: The scope that the send asked for: a region name, or ``*`` for unscoped.
    """

    def __init__(self, scope: str, reason: str) -> None:
        """Explain which scope MeshTerm could not set, and why.

        Args:
            scope: The region name, or :data:`~meshterm.core.regions.WILDCARD`.
            reason: The sentence to show (it already says that nothing was sent).
        """
        super().__init__(reason)
        self.scope = scope


#: The first companion firmware that accepts a session scope (``CMD_SET_FLOOD_SCOPE``), and
#: the first one that accepts the explicit unscoped override on it (``*``, sent as the flag
#: byte ``0x01``). Firmware before that release does not understand the command with the
#: flag. Thus an "unscoped" send would use the default scope instead.
SCOPE_FIRMWARE = (1, 10)
UNSCOPED_FIRMWARE = (1, 16)

#: The first firmware that has a stored default scope (``CMD_SET_DEFAULT_FLOOD_SCOPE``).
#: On older firmware, a plain flood is always unscoped.
DEFAULT_SCOPE_FIRMWARE = (1, 15)


def firmware_version(info: dict | None) -> tuple[int, int, int] | None:
    """The companion release as a tuple that can be compared, from its device-query ``ver``.

    The firmware reports its build as a string (``v1.15.0``, ``1.16.0-dev``, or a suffix of
    a vendor). Only the leading ``major.minor[.patch]`` has a meaning here.

    Args:
        info: A :meth:`Device.get_device_info` payload.

    Returns:
        ``(major, minor, patch)``, or ``None`` when the report has nothing in the form of a
        version (the simulator, or firmware older than the device query). In that case, the
        caller trusts the command itself to tell whether the firmware understands it.
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
    """The sentence for a companion that refused to set a scope for a send.

    Args:
        scope: The region name, or ``*``.
        exc: The exception that the command raised. The sentence keeps it for a person who
            debugs the problem.

    Returns:
        A sentence that gives the firmware that is necessary for the scope, and tells that
        nothing was sent.
    """
    if scope == REGION_WILDCARD:
        need = f"sending unscoped needs firmware {_version_text(UNSCOPED_FIRMWARE)} or newer"
    else:
        need = f"region scopes need firmware {_version_text(SCOPE_FIRMWARE)} or newer"
    return f"The radio refused scope {scope} ({exc}) — {need}. Nothing was sent."


#: ``ERR_CODE_NOT_FOUND``: the companion has no entry that matches what a command addressed.
_ERR_NOT_FOUND = 2

#: ``ERR_CODE_ILLEGAL_ARG``: an argument that the firmware refuses. For
#: ``CMD_SET_DEVICE_TIME``, it is a time earlier than the clock that the firmware already
#: has (refer to :class:`ClockAheadError`).
_ERR_ILLEGAL_ARG = 6

#: ``CMD_SET_AUTOADD_CONFIG``: the auto-add bitmask, and the hop limit (as an optional
#: second byte, which the ``meshcore`` library never sends).
_CMD_SET_AUTOADD_CONFIG = 58

#: The companion command that stores the default flood scope (firmware 1.15+).
#: :meth:`MeshCoreDevice.set_default_flood_scope` itself builds the bytes of this command,
#: not the library.
_CMD_SET_DEFAULT_FLOOD_SCOPE = 63

#: The companion command that sets, clears, or forces off the session flood scope (firmware
#: 1.10+, and 1.16+ for the force-unscoped mode). :meth:`MeshCoreDevice.set_flood_scope`
#: itself builds the bytes of this command, not the library.
_CMD_SET_FLOOD_SCOPE_KEY = 54

#: ``RESP_CODE_AUTOADD_CONFIG``: the reply to a read of the auto-add settings. The second
#: byte of its payload is the hop limit. The parser of the library removes this byte (it
#: keeps only the bitmask). Thus :meth:`MeshCoreDevice.get_autoadd_config` uses this code to
#: recognize the raw frame.
_RESP_AUTOADD_CONFIG = 25

#: A sentinel: MeshTerm has read no hop limit with the last auto-add bitmask.
_UNREAD = object()

#: The client-repeat frequencies (MHz) that the companion firmware accepts when a board
#: defines none of its own (``repeat_freq_ranges`` in MeshCore's
#: ``examples/companion_radio/MyMesh.cpp``).
_DEFAULT_REPEAT_FREQS: tuple[tuple[float, float], ...] = (
    (433.0, 433.0),
    (869.495, 869.495),
    (918.0, 918.0),
)

#: The maximum distance of a radio frequency from an accepted repeat range, where the
#: frequency still counts as in the range (MHz). Frequencies go over the protocol in kHz,
#: so half of one kHz is the full rounding error.
_REPEAT_FREQ_TOLERANCE_MHZ = 0.0005


def repeat_freq_allowed(freq: float, ranges: Iterable[tuple[float, float]]) -> bool:
    """Whether ``freq`` (MHz) is in one of the client-repeat ``ranges`` of the firmware.

    This is the firmware's own ``isValidClientRepeatFreq``, in MHz. The firmware does not
    let the companion relay on any other frequency.
    """
    tol = _REPEAT_FREQ_TOLERANCE_MHZ
    return any(low - tol <= float(freq) <= high + tol for low, high in ranges)


#: The meaning of each companion error code, in a clause that ends a sentence, in the form
#: "the device …". The protocol carries only the number and the ``ERR_CODE_*`` name of the
#: library (refer to ``meshcore.events.ErrorMessages``). That is diagnostic text, and it is
#: not for the user. :func:`reject_reason` changes it into the clause below.
_ERROR_REASONS = {
    1: "the firmware doesn't support that command",
    2: "the device has no contact with that key",
    3: "the device's table is full",
    4: "the device isn't in a state to do that",
    5: "the device hit a storage error",
    6: "the device rejected the request as malformed",
}


def _clip_utf8(text: str, limit: int) -> str:
    """Cut ``text`` to a maximum of ``limit`` UTF-8 bytes, and never cut a character in two.

    The firmware fields have a size in bytes, not in characters. Thus a name with an accent
    can be too long for a field, although it seems short enough.

    Args:
        text: The value to fit.
        limit: The size of the field, in bytes.

    Returns:
        ``text`` itself when it fits. If not, its longest prefix of whole characters that
        fits.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def error_code(result) -> int | None:  # noqa: ANN001
    """Return the companion error code in the event of a rejected command, if it has one.

    Args:
        result: The :class:`meshcore.events.Event` that a command returned (or ``None``).

    Returns:
        The ``error_code`` from the payload of the event, or ``None`` when the rejection had
        no code (no reply, or a payload with a different shape).
    """
    payload = getattr(result, "payload", {}) or {}
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload["error_code"])
    except (KeyError, TypeError, ValueError):
        return None


def reject_reason(result) -> str:  # noqa: ANN001
    """Explain a rejected command in one clause, for the end of a sentence to the user.

    A rejection arrives as ``{'error_code': 2, 'code_string': 'ERR_CODE_NOT_FOUND'}``. If
    MeshTerm puts this dict into a status line (all the raise sites did this in the past),
    the user sees a protocol constant and learns nothing. This function changes the code
    to plain words. It uses the raw payload only for a rejection with a code that we do not
    know.

    Args:
        result: The :class:`meshcore.events.Event` that a command returned (or ``None``).

    Returns:
        A lowercase clause that tells the cause, for example ``"the device's table is full"``.
    """
    code = error_code(result)
    if code in _ERROR_REASONS:
        return _ERROR_REASONS[code]
    if result is None:
        return "the device didn't answer"
    payload = getattr(result, "payload", {}) or {}
    return f"the device rejected it ({payload})"


#: The names of the exception classes that show that the link to the companion is lost. For
#: example, the device was unplugged, powered off, or moved out of range, or its port or
#: transport is gone in some other way. This is different from a usual failure of a command.
#: :func:`is_connection_lost` matches these names, so this module does not have to import
#: the optional ``pyserial`` and ``bleak`` dependencies (the ``--mock`` path installs neither
#: of them). ``SerialException`` is for the read and write failures of pyserial (also the
#: Windows ``ClearCommError`` and ``WriteFile`` variants). The ``Bleak*`` names are for a
#: Bluetooth link that was lost or a peripheral that went out of range. The ``OSError``
#: subclasses are for a link that the OS closed.
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

#: Lowercase message fragments that also show a lost link, for exceptions that are a plain
#: ``OSError`` or ``RuntimeError`` (for these, the type name alone does not prove the loss).
#: The fragments are specific, so that they do not match usual command timeouts.
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
    # The texts for a lost BLE link (bleak errors, and the reasons that the meshcore BLE
    # transport gives to its callback).
    "ble_transport_lost",
    "ble_write_failed",
    "ble_disconnect",
    "not connected to a ble device",
    "device is no longer connected",
)


def is_connection_lost(exc: BaseException) -> bool:
    """Return whether ``exc`` means that the serial link to the companion is lost.

    This function tells a *lost connection* (the device was unplugged, powered off, or its
    serial port is gone) from a usual command failure. Thus the interactive session can
    offer to reconnect, instead of only an error report. The function examines the full
    exception chain (``__cause__``/``__context__``). It matches the name of the exception
    type and the message text (refer to :data:`_CONNECTION_LOST_TYPES` and
    :data:`_CONNECTION_LOST_HINTS`). Thus this module does not have to import the optional
    ``pyserial`` dependency.

    Args:
        exc: The exception that a device operation raised.

    Returns:
        ``True`` if the exception (or an exception that it was raised from) seems to be a
        lost link.
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


#: The message fragments that a BLE stack uses when it cannot access a characteristic
#: without a bond. That is, the companion has a PIN, and we are not paired (or we gave the
#: wrong PIN). The GATT subscribe fails with one of these messages, not with a lost link.
#: Thus MeshTerm treats them as a separate "needs a PIN" case that the user can act on
#: (refer to :func:`_is_ble_auth_error`), and never as a lost connection. MeshTerm matches
#: them by text, so this module does not have to import ``bleak``.
_BLE_AUTH_HINTS = (
    "insufficient authentication",
    "insufficient authorization",
    "insufficient encryption",
    "not paired",
    # A failure of the pairing step itself, not of the subscribe that it guards: bleak's
    # ``pair()`` ("Could not pair with device: …") and BlueZ's AuthenticationFailed. Before
    # we added these, a refused pairing went through as an unknown failure. MeshTerm then
    # reported a device that "didn't answer as a MeshCore device".
    "could not pair",
    "authentication failed",
    "authenticationfailed",
    # ATT 0x0E, "Unlikely Error": the answer to the UART subscribe from a companion that got
    # a PIN after the bond was made. The bond is unauthenticated, because it is from the
    # time when the companion had no PIN. The encryption with the old key is successful,
    # and the MITM requirement of the characteristic refuses the subscribe. We saw this on
    # a uConsole (BlueZ bond Authenticated=0). MeshTerm uses this hint only during a
    # connect, where this message comes from the security layer. The hint sends the refusal
    # to the old-bond message and the PIN repair, and that repair corrects the problem.
    "unlikely error",
)


@dataclass(frozen=True)
class _BlePairing:
    """The result of the last Windows PIN pairing, kept so that a refusal can tell why.

    The pairing step is best-effort and returns a bool (refer to
    :meth:`MeshCoreDevice._pair_ble_windows`). The connect uses only this bool to find if
    it must try again. But a bool cannot tell the user if the PIN was wrong, if the device
    refused, or if Windows could not reach the device. These causes have different repairs.

    Attributes:
        outcome: ``"reused"`` (Windows trusted a bond that exists), ``"paired"``,
            ``"absent"`` (Windows could not reach the device), ``"failed"`` (the ceremony
            returned a status that is not success), or ``"error"`` (the WinRT call raised).
        status: The status name of the ceremony, in lowercase with spaces
            (``"authentication failure"``), or the exception text for ``"error"``. It is
            empty in other cases.
    """

    outcome: str
    status: str = ""


def _ble_host() -> str | None:
    """The name of the OS whose pairing MeshTerm itself runs, for a message.

    It is ``None`` on other OSes.
    """
    if sys.platform == "win32":
        return "Windows"
    if sys.platform.startswith("linux"):
        return "Linux"
    return None


def _ble_forget(where: str) -> str:
    """How to remove the bond of a device on this OS, as a clause for a repair message."""
    if sys.platform.startswith("linux"):
        return f"remove it with `bluetoothctl remove {where}`"
    return "remove it in Settings > Bluetooth"


#: The pairing statuses that mean that the PIN itself was wrong: the WinRT names, then the
#: BlueZ name. (In BlueZ, AuthenticationFailed after the agent was asked for the PIN is a
#: passkey mismatch.)
_PAIRING_WRONG_PIN = frozenset(
    {"authentication failure", "invalid ceremony data", "authentication failed"}
)

#: The pairing statuses that mean that the device (not the PIN) refused the pairing. For
#: example, the device is busy with another host, it has an old bond, or it does not answer
#: in time.
_PAIRING_REFUSED = frozenset(
    {
        "connection rejected",
        "too many connections",
        "remote device has association",
        "not ready to pair",
        "rejected by handler",
        "authentication timeout",
        "protection level could not be met",
        # The BlueZ names of the same refusals.
        "authentication rejected",
        "authentication canceled",
        "connection attempt failed",
        "in progress",
    }
)


#: The text of a link that opened and then was lost before the session started. The BlueZ
#: backend of bleak says "failed to discover services, device disconnected" when it loses
#: the peer during service discovery. On a Cardputer Zero, this was an HCI Connection
#: Timeout (0x08) after 1.7 s, at all signal strengths, before a security exchange started.
#: The next try connected. It is a short radio problem, so MeshTerm tries again, as for a
#: link that never opened.
_BLE_DROP_HINTS = ("device disconnected",)


def _chain_says(exc: BaseException, hints: tuple[str, ...]) -> bool:
    """Whether ``exc``, or an exception that it was raised from, contains one of ``hints``.

    The function examines the full ``__cause__``/``__context__`` chain. Thus it also
    recognizes a bleak error in a wrapper of the meshcore transport.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if any(hint in text for hint in hints):
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_ble_auth_error(exc: BaseException) -> bool:
    """Return whether ``exc``, or an exception in its chain, is a BLE authentication rejection.

    It matches :data:`_BLE_AUTH_HINTS` along the exception chain. Thus it also recognizes
    a ``BleakGATTProtocolError`` in a wrapper of the meshcore transport.

    Args:
        exc: The exception that the open of the Bluetooth connection raised.

    Returns:
        ``True`` if the failure is a pairing that is missing or rejected, not a lost link.
    """
    return _chain_says(exc, _BLE_AUTH_HINTS)


def _is_ble_link_drop(exc: BaseException) -> bool:
    """Whether the link open failed because the link was lost, not because security refused it.

    An authentication refusal can also end in a disconnect. Thus a failure that seems to be
    an authentication refusal is never a lost link. A wrong or missing PIN must go to the
    code that handles the PIN, never to a retry.
    """
    return not _is_ble_auth_error(exc) and _chain_says(exc, _BLE_DROP_HINTS)


#: The ``errno`` values of a failed serial open, in groups by their meaning for the user.
_ERRNO_DENIED = {1, 13}  # EPERM, EACCES
_ERRNO_BUSY = {11, 16}  # EAGAIN (pyserial's exclusive lock), EBUSY
_ERRNO_GONE = {2, 6, 19}  # ENOENT, ENXIO, ENODEV

_ERRNO_IN_TEXT = re.compile(r"\[Errno (\d+)\]")


def _serial_open_message(port: str, exc: BaseException) -> str | None:
    """Tell why a serial port did not open, or ``None`` if the cause is not a known one.

    pyserial puts the OS error into a ``SerialException``, and it usually does not set the
    ``errno`` of that exception. Thus the function first looks in the chain for a real
    ``OSError``, and then for the ``[Errno N]`` in the text. Windows says "Access is
    denied" for a port that another program holds. On Windows, this is a busy port, not a
    permission problem.

    Args:
        port: The port, as the message names it.
        exc: The exception that the open raised.

    Returns:
        The sentence that the user can act on, or ``None`` to let the original error
        through.
    """
    code: int | None = None
    text = ""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text += " " + str(current).lower()
        if code is None and isinstance(current, OSError) and current.errno:
            code = current.errno
        current = current.__cause__ or current.__context__
    if code is None and (found := _ERRNO_IN_TEXT.search(text)):
        code = int(found.group(1))
    windows_busy = sys.platform == "win32" and ("access is denied" in text or code == 13)
    if windows_busy or code in _ERRNO_BUSY or "resource busy" in text or "exclusively lock" in text:
        modem = (
            " On Linux this is often ModemManager, which probes new USB serial devices: "
            "`sudo systemctl stop ModemManager` and try again."
            if sys.platform.startswith("linux")
            else ""
        )
        return (
            f"{port} is in use by another program — the MeshCore app, a flasher, or a serial "
            f"monitor. Close it and try again.{modem}"
        )
    if code in _ERRNO_DENIED or "permission denied" in text:
        return (
            f"no permission to open {port}. Your user needs to be in the group that owns it "
            "(dialout on Ubuntu and Debian): `sudo usermod -aG dialout $USER`, then log out "
            "and back in."
        )
    if code in _ERRNO_GONE or "cannot find the file" in text or "no such file" in text:
        return f"{port} isn't there — the device was unplugged, or came back under another name."
    return None


#: The ``errno`` values of a failed TCP connect when no route goes to the host: the POSIX
#: constants, and the Winsock constants (WSAENETUNREACH, WSAEHOSTUNREACH), which Windows
#: reports without a change.
_ERRNO_UNREACHABLE = {errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065}


def _tcp_open_message(host: str, port: int | None, exc: BaseException) -> str | None:
    """Tell why a TCP connection to a companion did not open, or ``None`` if not recognized.

    In the past, MeshTerm reported each of these causes as "no response from a MeshCore
    companion". Thus the user examined the companion, but the fault was the address, the
    port, or the network between them. Each of these causes has a different repair.

    Args:
        host: The host, as the message names it.
        port: The TCP port.
        exc: The exception that the open of the socket raised.

    Returns:
        The sentence that the user can act on, or ``None`` to report the error as not
        recognized.
    """
    if isinstance(exc, socket.gaierror):
        return f"couldn't find a host named {host} — check the spelling, or use its IP address."
    if isinstance(exc, ConnectionRefusedError):
        return (
            f"nothing is accepting connections on port {port} at {host} — check the port "
            "number, and that the companion, or the bridge in front of it, is running."
        )
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return (
            f"{host}:{port} closed the connection as it opened — a network companion serves "
            "one client at a time, so another may already be connected to it."
        )
    if isinstance(exc, OSError) and exc.errno in _ERRNO_UNREACHABLE:
        return (
            f"there's no route to {host} from this computer — check the address, and that "
            "this computer is on the companion's network (or its VPN)."
        )
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return (
            f"no answer from {host} on port {port} — check that it is powered on and on this "
            "computer's network. A firewall can also hold a connection without refusing it."
        )
    return None


def _ble_stalled_message(where: str, seconds: float | None = None) -> str:
    """Tell that a Bluetooth connect used all its time, and what usually blocks a connect.

    Args:
        where: The device, as the message names it.
        seconds: The time window that the connect used, when the caller set one.

    Returns:
        The sentence that the user can act on.
    """
    took = f"took longer than {seconds:.0f}s" if seconds else "timed out"
    return (
        f"connecting to {where} over Bluetooth {took} — the link, pairing, or service "
        "discovery stalled. Move closer, restart the device, and try again."
    )


def _error_words(exc: BaseException) -> str:
    """The error message, or the type name if it has no message (a bare ``TimeoutError``)."""
    return str(exc).strip() or type(exc).__name__


#: The failures that MeshTerm did not recognize and whose traceback is already in the log.
#: The reconnect dialog tries again every few seconds while the cause still refuses it. One
#: traceback gives all the information that the next three hundred would give.
_TRACED: set[str] = set()


def _unrecognised(what: str, exc: BaseException) -> UnrecognisedConnectError:
    """Make the error for a connect failure that has no MeshTerm sentence, and log all of it.

    The function logs the failure at the warning level, with the traceback the first time
    that it occurs, because the dialog then refers the user to the log. The sentence has the
    words of the error, and the log has the location where they came from.

    Args:
        what: The action that failed, as the start of the sentence ("couldn't open COM5").
        exc: The error that MeshTerm did not recognize.

    Returns:
        The error to raise, from ``exc``.
    """
    words = _error_words(exc)
    message = f"{what}: {words}"
    _log.warning("%s", message, exc_info=exc if message not in _TRACED else None)
    _TRACED.add(message)
    return UnrecognisedConnectError(message)


class _ConnectingClients(list):
    """Each bleak client for which a ``BLEConnection`` reported a disconnect during its connect.

    Attributes:
        connecting: Whether ``connect()`` still runs. While it runs, this list stores a
            disconnect, and does not pass it on to meshcore (refer to
            :func:`_hold_disconnects_while_connecting`).
        hung_up: Set when the link was lost after BlueZ asked for the PIN. That is, the
            companion refused the pairing, and no retry in bleak will undo this refusal.
    """

    connecting = True

    def __init__(self, clients: Iterable[object] = ()) -> None:
        """Start with ``clients`` (usually none) and no hang-up."""
        super().__init__(clients)
        self.hung_up = asyncio.Event()


def _hold_disconnects_while_connecting(  # noqa: ANN001
    connection, pin_asked: Callable[[], bool] = lambda: False
) -> _ConnectingClients:
    """Prevent meshcore from removing its bleak client while that client still connects.

    On Linux, BlueZ answers a link try that did not synchronize
    (``le-connection-abort-by-local``, HCI 0x3e), and bleak tries the link again inside its
    own ``connect()``. On a Raspberry Pi radio, this occurs three to seven times for each
    connect. Each failed try calls the disconnect callback of the client. meshcore's
    ``BLEConnection.handle_disconnect`` then resets ``self.client`` to the value that the
    caller gave, which is ``None``. The next retry of bleak is then successful on a client
    that nothing holds. meshcore's ``start_notify`` gets ``None``, its connect stops as "not
    established", and the live link has no owner.

    The companion, which is connected to us, then stops advertising. Thus the retry cannot
    find it, and MeshTerm tells the user that it is out of range. We measured this with a
    companion on a uConsole and btmon: three runs of three.

    Thus, while ``connect()`` runs, this function only stores a disconnect. bleak raises an
    error itself if it cannot make the link. After ``connect()`` returns,
    disconnects go to meshcore as before, because the client continues to call this same
    wrapper.

    The only lost link that bleak must not try again is a companion that hangs up on a
    pairing. A companion with a PIN (an ESP32 companion, measured on a Cardputer Zero)
    answers the UART subscribe with a request for the PIN. If the PIN is refused or wrong,
    the companion disconnects, instead of a refusal of the subscribe. When this function
    held back that disconnect, ``connect()`` waited until its timeout on a dead link, and it
    never asked for the PIN. Thus a lost link after BlueZ asked for the PIN sets
    ``hung_up``, and the connect stops on it as a pairing failure.

    Args:
        connection: The ``meshcore.BLEConnection`` that will connect.
        pin_asked: Whether BlueZ has asked the agent of this connect for the PIN yet.

    Returns:
        The record of the clients that reported a disconnect. The caller clears its
        ``connecting`` flag after ``connect()`` has returned or raised.
    """
    seen = _ConnectingClients()
    forward = getattr(connection, "handle_disconnect", None)
    if forward is None:  # a transport without a disconnect handler has nothing to hold back
        return seen

    def _handle_disconnect(client) -> None:  # noqa: ANN001 - a bleak client
        seen.append(client)
        if seen.connecting:
            if pin_asked():
                _log.debug("BLE companion hung up on the pairing it asked for")
                seen.hung_up.set()
                return
            _log.debug("BLE link attempt dropped while connecting; bleak retries it")
            return
        forward(client)

    # BLEConnection gives ``self.handle_disconnect`` to each BleakClient that it makes in
    # ``connect()``. Thus an instance attribute that we set now is the callback of each
    # client.
    connection.handle_disconnect = _handle_disconnect
    return seen


class _PairingHungUp(Exception):
    """The companion disconnected after it asked for the PIN: a refused or missing pairing.

    Its message has the words of an authentication failure (:data:`_BLE_AUTH_HINTS`),
    because it is one. Thus the part of the connect that handles the PIN continues from
    here, not the link retry.
    """


async def _unless_hung_up(connect: Awaitable[Any], hung_up: asyncio.Event) -> Any:
    """Await ``connect``, unless the companion hangs up on its pairing first.

    Args:
        connect: The meshcore ``connect()`` coroutine.
        hung_up: The event that :func:`_hold_disconnects_while_connecting` sets on that
            hang-up.

    Returns:
        The value that ``connect`` returned.

    Raises:
        _PairingHungUp: If the hang-up came first. ``connect`` is then cancelled.
    """
    task = asyncio.ensure_future(connect)
    waiter = asyncio.ensure_future(hung_up.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            return task.result()
        raise _PairingHungUp("authentication failed: the companion hung up on the pairing")
    finally:
        waiter.cancel()
        if not task.done():
            task.cancel()
            with suppress(BaseException):
                await task


class _WriteRefusals(list):
    """Each exception that a ``BLEConnection`` write raised, the oldest first."""

    def refusal(self) -> BaseException | None:
        """The first exception for a write that was refused because of no bond, or ``None``."""
        return next((exc for exc in self if _is_ble_auth_error(exc)), None)


def _record_write_refusals(connection) -> _WriteRefusals:  # noqa: ANN001
    """Keep the error of a refused write, which meshcore's ``send`` logs and then forgets.

    The stock companion firmware guards both halves of its UART service with ENC+MITM. Thus
    the companion refuses a host without a bond at the notify subscribe. This error comes
    out of ``connect()``, where :meth:`MeshCoreDevice._open_ble` changes it into a pairing.
    The standalone T-Deck firmwares (MeshOS, wadamesh) guard only the **write**
    characteristic. The subscribe is successful, and the refusal comes on the first write
    of the identity handshake ("Insufficient Encryption" without a bond, "Insufficient
    Authentication" over a Just Works bond).

    ``BLEConnection.send`` catches that error, logs "BLE write failed", and returns
    ``False``. The handshake then returned empty, and MeshTerm read the device as "not a
    MeshCore companion". There was no PIN pairing, no repair of the old bond, and no wait
    for the macOS Passkey, because no later step got an authentication error. We checked
    this with a T-Deck on MeshOS 1.3.0.

    Thus a wrapper around the write stores the exception as it goes through (the exception
    still goes on to ``send``, which operates as before). Then
    :meth:`MeshCoreDevice._connect_owned_ble` raises the stored refusal when the handshake
    returned empty. Both firmwares then use the same pairing path.

    Args:
        connection: The ``meshcore.BLEConnection`` that will connect.

    Returns:
        The record of the write failures. It is empty until a failure occurs.
    """
    seen = _WriteRefusals()
    write = getattr(connection, "_write_locked", None)
    if write is None:  # a meshcore without the hook: the handshake operates as before
        return seen

    async def _write_locked(data) -> None:  # noqa: ANN001 - bytes-like, as meshcore sends
        try:
            await write(data)
        except Exception as exc:
            seen.append(exc)
            raise

    connection._write_locked = _write_locked
    return seen


def _held_client(connection, seen: _ConnectingClients):  # noqa: ANN001, ANN202
    """The bleak client to keep for the teardown.

    It is the client of the connection, or else the last client that the connection lost.
    """
    client = getattr(connection, "client", None)
    if client is not None:
        return client
    return next((c for c in reversed(seen) if getattr(c, "is_connected", False)), None)


def serial_port_present(port: str) -> bool:
    """Return whether the OS now lists a serial port with the name ``port``.

    This is the main liveness signal for an unplug during a session. After the cable is
    pulled, the ``meshcore`` client continues to serve cached data and never raises an
    error (we checked this on hardware: a command still "succeeds", and only returns
    ``None``). Thus MeshTerm cannot use a failed command to find the unplug. But the OS
    port list removes the device at the moment that it is removed. The list of ports only
    reads the device table of the OS. It never opens the port, so it is safe to poll it
    while another handle holds the companion open.

    Args:
        port: The name of the serial port on which the device was opened (for example
            ``COM11`` or ``/dev/ttyUSB0``).

    Returns:
        ``True`` if a port with that exact name is present. Also ``True`` if MeshTerm
        cannot find out (pyserial is missing, or the query failed), so that a short lookup
        problem never gives a false "disconnected" alarm.
    """
    try:
        from serial.tools import list_ports
    except Exception:  # noqa: BLE001 - no pyserial (for example --mock): unknown, assume it is up
        return True
    try:
        if any(info.device == port for info in list_ports.comports()):
            return True
    except Exception:  # noqa: BLE001 - an enumeration failure must not fake a disconnect
        return True
    # pyserial's Linux ``comports()`` never lists a soldered platform-bus UART (for example,
    # an SoC port such as ``/dev/ttyS1`` on the Luckfox Lyra). But its device file exists
    # for exactly as long as the port exists. The device file of a USB serial port is
    # removed from the filesystem at the moment that the cable is pulled. Thus a ``/dev``
    # character device file that exists is a good presence signal, and it never hides a
    # real unplug. It is also safe on Windows: COM names are not filesystem paths, so
    # MeshTerm skips this branch there.
    try:
        import os
        import stat

        if port.startswith("/dev/") and os.path.exists(port):
            return stat.S_ISCHR(os.stat(port).st_mode)
    except Exception:  # noqa: BLE001 - a stat hiccup must not fake a disconnect
        return True
    return False


class Device(ABC):
    """An abstract companion device, with the operations that MeshTerm uses.

    Each implementation manages its own connection lifecycle. It changes the raw protocol
    events into the domain models in :mod:`meshterm.core.models`.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection to the device. Idempotent."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection and release the resources. Idempotent."""

    async def link_present(self) -> bool:
        """Return whether the transport link below is still up (best-effort).

        This is a fast liveness probe that has no side effects. It never transmits, and it
        never opens a new handle. Thus the interactive session can poll it while the device
        is in use, to find a lost link during the session (a serial cable pulled, a
        companion powered off, a BLE peripheral out of range). Each transport uses the
        signal that correctly shows its link state (the OS port list for serial, the
        connection flag of the BLE client for Bluetooth). It returns ``True`` when it
        cannot find the presence, so a short lookup problem never gives a false disconnect.

        Returns:
            ``True`` if the link seems up (or if it cannot be checked), ``False`` if it is
            gone.
        """
        return True

    @abstractmethod
    async def get_self_info(self) -> dict:
        """Return the identity and the radio settings of the connected device.

        Returns:
            A dict with a minimum of ``name`` and, when available, ``tx_power`` and the
            radio parameters (``freq``, ``bw``, ``sf``, ``cr``).
        """

    @abstractmethod
    async def get_device_info(self) -> dict:
        """Return the hardware and build identity of the connected device, from the firmware.

        This is a different protocol query from :meth:`get_self_info`. Self-info reports the
        identity of the node and its radio tuning. This method reports what the box itself
        is: a ``model`` string (for example ``"Seeed Tracker T1000-E"``, which matches a
        MeshCore firmware ``variant``), and the firmware ``ver``/``fw_build``. It is the
        only place where the vendor and the model are available. BLE advertisements have
        no manufacturer data for these boards. Most boards use a random address with
        no IEEE OUI to look up, and they have no GATT Device Information Service. Thus the
        model must come from MeshCore's own application layer.

        This method is best-effort. Firmware that is older than the device query answers
        with an empty payload, not with an error. Thus callers must treat a missing
        ``model`` as unknown.

        Returns:
            A dict with ``model``, ``ver``, and ``fw_build``, when available. It can be
            empty.
        """

    @abstractmethod
    async def get_contacts(self) -> list[Contact]:
        """Return the contacts that the device knows.

        Returns:
            The list of :class:`Contact` records that the device stores now.
        """

    @abstractmethod
    async def add_contact(self, node: Contact) -> None:
        """Add (or update) a contact in the contact table of the device.

        This is the inverse of :meth:`remove_contact`. It is also the repair for a node that
        MeshTerm knows but that the firmware has forgotten. The address of a direct message
        comes from the contact entry of the device itself. Thus MeshTerm cannot send a
        message to a contact that is not in that table (:class:`ContactNotOnDeviceError`)
        until it writes the contact back. The contact has all the data that the entry must
        have: its public key, name, type, and last advertised position. The method adds it
        with no learned route, so the first message floods exactly as for a node that the
        device just heard.

        Args:
            node: The contact to write. It must have a full public key.

        Raises:
            DeviceCommandError: If the contact has no public key for the address, or the
                device rejected the write (most often, because the contact table is full).
        """

    @abstractmethod
    async def remove_contact(self, node: Contact) -> None:
        """Delete a contact from the contact table of the device.

        The method addresses the contact by its public key, so the contact must have one (a
        contact heard as an advert always has one). The node stays a node: its reception
        history and its overheard traffic do not change. The method only removes it from
        the list of the added contacts on the device, to which you can send messages.

        Args:
            node: The contact to remove.

        Raises:
            ContactNotOnDeviceError: If the device has no contact with that key. This is
                not a failed removal, but a removal with nothing more to do. Thus callers
                remove the contact from what MeshTerm remembers, and tell the user.
            DeviceCommandError: If the contact has no public key for the address, or the
                device rejected the removal for any other reason.
        """

    @abstractmethod
    async def get_tx_power(self) -> int | None:
        """Return the current TX power level, or ``None`` if it is not known."""

    @abstractmethod
    async def set_tx_power(self, value: int) -> None:
        """Set the transmit power of the radio.

        Args:
            value: The TX power level. The caller clamps it to the valid range of the
                device.
        """

    # -- remote administration (tune a node on which we have admin rights) -------

    @abstractmethod
    async def admin_login(self, node: Contact, password: str) -> LoginResult:
        """Authenticate as administrator on a remote node.

        Args:
            node: The contact to log in to. Its ``public_key`` addresses the node.
            password: The admin password of the node.

        Returns:
            A :class:`LoginResult`. It is truthy only for :attr:`~LoginResult.ACCEPTED`.
            Thus a caller that only asks "am I in?" can still write
            ``if not await device.admin_login(...)``. A caller that acts on the failure
            must tell :attr:`~LoginResult.REFUSED` (the node said no: the password is
            wrong) from :attr:`~LoginResult.NO_REPLY` (nothing came back: the password is
            not proved correct, and not proved wrong).
        """

    @abstractmethod
    async def room_login(self, room: Contact, password: str) -> RoomLogin:
        """Log in to a room server, as a member or as its admin, and tell the access it gave.

        This is the same exchange as :meth:`admin_login`. A room server has one login, and
        the password selects the role. The answer of a room has one more item: the access
        that the room gave (:class:`~meshterm.core.models.RoomAccess`). The companion adds
        the time of the last post that it already has from this room. Thus the room
        answers with each post after that time, one at a time, as usual received messages.

        A room never says no. A wrong password gets no reply at all (unless the owner of
        the room lets all users in as read-only). Thus a refusal is
        :attr:`~LoginResult.NO_REPLY` here, and a caller must not read that result as only
        "unreachable". A blank password asks the room if it already knows us. It knows its
        admins, and all users who logged in after it last restarted.

        Args:
            room: The room server to log in to. Its ``public_key`` addresses it.
            password: The room password, the admin password, or ``""``.

        Returns:
            How the login ended, and the access that the room gave when it accepted the
            login.
        """

    @abstractmethod
    async def reset_route(self, node: Contact) -> None:
        """Forget the route that the radio learned to ``node``, so its next message floods.

        The radio learns a route from the path on which a flood arrived. The route becomes
        old when the mesh changes (a repeater moved, was switched off, or another repeater
        is now heard better). After that, each message sent along the route is lost with no
        error. The action to forget the route is local (nothing is transmitted). The next
        message to the node floods the full mesh, and the radio learns a new route from the
        answer. MeshCore's own apps call this *reset path*.

        Args:
            node: The contact whose route to forget.

        Raises:
            ContactNotOnDeviceError: If the radio has no contact for the node.
            DeviceCommandError: If the radio refused for any other reason.
        """

    @abstractmethod
    async def send_remote_command(
        self, node: Contact, command: str, *, timeout: float = 8.0
    ) -> str | None:
        """Send one CLI command to a remote node where we are logged in, and wait for its reply.

        This is the generic primitive of remote administration. Repeaters and room servers
        are set up through their text CLI (``get``/``set``/``advert``/…), in admin
        messages. Each remote operation at a higher level is a form of this method. An
        authenticated session is necessary (call :meth:`admin_login` first). The firmware
        silently ignores commands from unknown nodes, and this shows as a ``None`` reply.

        Args:
            node: The remote contact (it must already be logged in).
            command: The CLI command text, for example ``"set txdelay 5"``.
            timeout: The seconds to wait for the reply of the node.

        Returns:
            The reply text, or ``None`` if the node did not answer in time.

        Raises:
            DeviceCommandError: If the companion rejected the send command itself.
        """

    @abstractmethod
    async def get_remote_tx_power(self, node: Contact) -> int | None:
        """Read the current transmit power of a remote (admin) node.

        Args:
            node: The contact to query (it must already be logged in).

        Returns:
            The TX power of the node in dBm, or ``None`` if MeshTerm could not read it.
        """

    @abstractmethod
    async def set_remote_tx_power(self, node: Contact, value: int) -> None:
        """Set the transmit power of a remote (admin) node.

        Args:
            node: The contact to change (it must already be logged in).
            value: The TX power in dBm.

        Raises:
            DeviceCommandError: If the node rejected the command.
        """

    @abstractmethod
    async def fetch_neighbours(self, node: Contact) -> list[NeighbourInfo]:
        """Ask a remote node for its neighbour table: the nodes it hears directly, and how well.

        This is a second point of view for the topology graph. Each entry is a link
        ``node ↔ neighbour``, with the SNR measured at the remote node. This includes nodes
        from which we have never received anything ourselves. An authenticated session is
        necessary (call :meth:`admin_login` first). The firmware silently ignores the
        request from guests, and here this shows as a timeout.

        Args:
            node: The contact to query (it must already be logged in).

        Returns:
            The reported neighbour entries. The list is empty when the table of the node is
            empty. This is a usual answer: repeaters forget their neighbours at a reboot,
            and learn them again only when adverts arrive.

        Raises:
            DeviceCommandError: If the node never answered (not logged in, out of
                reach, or firmware without neighbour tables).
        """

    @abstractmethod
    async def request_regions(self, node: Contact) -> list[str]:
        """Ask a repeater for which regions it relays floods (firmware 1.12+).

        This is an anonymous request (with no login). A repeater answers it only when it
        arrives direct or zero-hop. The repeater ignores a flooded copy (the firmware lets
        ``REGIONS`` through only if ``isRouteDirect()``). Thus, for the exchange, the
        library sets a contact with no route to zero hops. In practice, that means a
        neighbour, or a contact with a learned route. The repeater also has a rate limit
        for this request. Thus this is a one-time question, never a poll.

        Args:
            node: The repeater to ask.

        Returns:
            The region names that it gave, bare, in its order. ``*`` is first when it also
            relays unscoped floods (refer to
            :func:`~meshterm.core.regions.parse_region_list`).

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
        """Run one path trace to ``target`` and return the SNR of each hop.

        Args:
            target: The name or the key prefix of the destination node.
            path: An optional explicit path to force, as a comma-separated string of
                single-byte hex hashes (for example ``"3d,f2,3d"``). When ``None``, the
                connection finds a path from the learned route of the contact. (The
                firmware itself never routes a trace: it walks only an explicit path.) Only
                for unknown targets, it uses no path.
            timeout: The seconds to wait for the trace reply. ``None`` (the default) sets
                the wait from the route. A trace must go along the full path out and back,
                so a long walk gets proportionally more time to come back
                (:func:`~meshterm.core.tracing.trace_timeout`).

        Returns:
            A :class:`TraceResult`. Its ``success`` is ``False`` on a timeout.
        """

    # -- passive event stream ----------------------------------------------------

    @abstractmethod
    async def subscribe_events(self, on_event: EventCallback) -> Unsubscribe:
        """Start a stream of the unsolicited received events of the device, without blocking.

        The subscription is for all the events that the companion gives on its own:
        overheard adverts and telemetry (as
        :attr:`~meshterm.core.events.EventKind.OBSERVATION` events), received direct and
        channel text messages (:attr:`~meshterm.core.events.EventKind.MESSAGE`), and
        delivery acknowledgements (:attr:`~meshterm.core.events.EventKind.ACK`). Each event
        goes to ``on_event`` as a :class:`~meshterm.core.events.MeshEvent` when it arrives.
        The delivery continues in the background until a call to the returned callable
        stops it. The radio never gets a request to transmit. It only listens. The
        always-on :class:`~meshterm.services.event_hub.EventHub` is built on this
        primitive.

        Note:
            This stream has only unsolicited events. The command that sent a request
            waits for the replies that correlate to that request (the ``TRACE_DATA`` of a
            trace, a login result). Thus those flows operate with or without a live
            subscription.

        Args:
            on_event: The callback that receives each :class:`MeshEvent` when it is heard.

        Returns:
            A callable with no arguments that stops the stream and releases the
            subscription.
        """

    # -- messaging ---------------------------------------------------------------

    @abstractmethod
    async def send_direct_message(self, contact: Contact, text: str) -> Delivery:
        """Send a direct text message to a contact, and wait some time for its ack.

        Args:
            contact: The recipient. Its ``public_key`` addresses the message.
            text: The message body.

        Returns:
            The :class:`~meshterm.core.models.Delivery`: the ack code that the radio
            expects, and the ack itself if it arrived during the wait. The radio still
            pushes an ack that comes later, as an ``ACK`` event with the same code.

        Raises:
            DeviceCommandError: If the companion rejected the send command itself.
        """

    @abstractmethod
    async def send_channel_message(self, index: int, text: str) -> None:
        """Broadcast a text message on a channel slot.

        Args:
            index: The zero-based channel slot on which to transmit.
            text: The message body.

        Raises:
            DeviceCommandError: If the companion rejected the send.
        """

    # -- transmission in a scope --------------------------------------------------------

    @property
    def transmit_lock(self) -> TransmitLock:
        """The lock that each transmission holds while it gives its command to the companion.

        MeshTerm makes the lock at the first use, not in ``__init__``, so an implementation
        does not have to remember to call the parent class for it. Refer to
        :mod:`~meshterm.core.transmit_lock` for the reason why it exists: a channel scope
        is a window on the one session scope of the companion, and nothing else can go
        through that window.
        """
        lock = getattr(self, "_transmit_lock", None)
        if lock is None:
            lock = TransmitLock()
            self._transmit_lock = lock
        return lock

    @asynccontextmanager
    async def transmitting(self) -> AsyncIterator[None]:
        """Hold the transmit lock for one hand-over, and repair a scope left by a failed restore.

        Each send of an implementation that can go out as a flood holds this lock around
        the command that transmits, and only around that command. A wait for an ack or a
        reply after the command is not a transmission. It must not block a scoped channel
        send.

        A scoped send whose restore failed (refer to :meth:`send_channel_in_scope`) leaves
        the session scope of the companion on the region of a channel, and each later
        flood would use it. The first transmission that comes through here after that puts
        the scope back before it sends anything. If the companion still does not accept
        the reset, the send goes out in the scope that it had, with a log entry, instead
        of no send at all. This is because a refusal of each transmission after that time
        would be the worse failure.
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
        """Broadcast on a channel in a given scope, and leave the session scope as it was.

        This is the only way that MeshTerm sends a channel message. ``None`` is the plain
        send, which goes out in the default scope of the companion, whatever it is. A
        region name or ``*`` uses the three-step window from which the firmware makes a
        per-channel scope: set the session scope, send, and restore it (``None``, back to
        the default). All three steps are under :attr:`transmit_lock`, so no other flood
        can go out between the steps.

        A refusal occurs before MeshTerm sends anything (:class:`FloodScopeError`). A
        scoped message never goes out silently unscoped, or in the default scope.

        * For a region name, firmware :data:`SCOPE_FIRMWARE` is necessary. Older firmware
          answers the command with an error, and MeshTerm takes the error as a refusal
          (which it is).
        * For ``*``, firmware :data:`UNSCOPED_FIRMWARE` is necessary to override a default
          scope. Older firmware can still send unscoped when there is no default to
          override. Firmware before :data:`DEFAULT_SCOPE_FIRMWARE` has no default scope,
          and later firmware can have no default set. In that case, the plain send is the
          unscoped send, and it goes out. MeshTerm refuses only when a default is in fact
          set.

        Args:
            index: The zero-based channel slot on which to transmit.
            text: The message body.
            scope: A region name, :data:`~meshterm.core.regions.WILDCARD` for unscoped, or
                ``None`` for the default of the device.

        Raises:
            FloodScopeError: If the companion cannot send in ``scope``. Nothing was sent.
            ~meshterm.core.regions.RegionNameError: If ``scope`` is not a region name that
                the firmware can hold.
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
                except Exception as exc:  # noqa: BLE001 - the message went out. Repair later.
                    _log.warning("couldn't clear send scope %r after a channel send: %s", bare, exc)
                    self._scope_leaked = bare

    async def _unscoped_is_plain(self) -> bool:
        """Whether a plain flood is already unscoped, so that ``*`` is possible with no override.

        If ``*`` is not possible at all, this method refuses (it raises the error below).
        MeshTerm asks only firmware that is too old to accept the override. It trusts
        firmware that reports no version to answer the override command itself.

        Returns:
            ``True`` when the plain send is unscoped. ``False`` when the override is
            available and MeshTerm must use it.

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
        except Exception:  # noqa: BLE001 - the default is unknown, so unscoped is not certain
            default = "?"
        if not normalize_region(default or ""):
            return True
        raise FloodScopeError(
            REGION_WILDCARD,
            f"This radio floods everything under its default scope {default}, and only "
            f"firmware {_version_text(UNSCOPED_FIRMWARE)} or newer can send one message "
            "unscoped over it. Nothing was sent.",
        )

    # -- settings: more reads ---------------------------------------------------

    @abstractmethod
    async def get_tuning(self) -> dict:
        """Return the radio tuning parameters, in their real units.

        Returns:
            A dict with ``rx_delay`` (float seconds) and ``airtime_factor`` (float).
            The firmware stores both as floats, and sends them over the protocol scaled
            ×1000. The implementations remove that scaling, so callers see only the real
            values.
        """

    @abstractmethod
    async def get_autoadd_config(self) -> int | None:
        """Return the contact auto-add bitmask, or ``None`` if the firmware is older than it.

        This is a more detailed relative of :meth:`set_manual_add_contacts`: a bitmask of
        the advert types that the firmware adds to the contacts automatically.
        """

    async def get_autoadd_max_hops(self) -> int | None:
        """Return the auto-add hop limit, or ``None`` when the firmware does not report one.

        It is the second byte of the auto-add settings of the firmware
        (``autoadd_max_hops``, with a maximum of 64). It is not abstract: a device without
        the field has nothing to say, and the snapshot leaves its row unread instead of a
        failure.
        """
        return None

    async def get_allowed_repeat_freqs(self) -> list[tuple[float, float]]:
        """Return the frequency ranges (MHz, inclusive) on which client repeat can be on.

        The firmware does not relay out of these ranges (``isValidClientRepeatFreq``). Thus
        the editor can check before it stages a change, instead of after a refusal. The
        list is empty when the firmware does not tell. This means unknown, not nowhere.
        """
        return []

    @abstractmethod
    async def get_default_flood_scope(self) -> str | None:
        """Return the name of the stored default flood scope (``""`` when it is not set).

        Returns:
            The ``#scope`` name that limits flood routing, an empty string when no scope
            is set, or ``None`` if the firmware is older than flood scopes.
        """

    @abstractmethod
    async def get_time(self) -> int | None:
        """Return the device clock as a UNIX epoch timestamp, or ``None`` if not known."""

    @abstractmethod
    async def get_battery(self) -> dict:
        """Return the battery status (and the storage status, when the firmware reports it).

        Returns:
            A dict with ``level`` (millivolts) and, on firmware that reports storage,
            ``used_kb``/``total_kb``. It is empty when the firmware does not support the
            read.
        """

    async def get_hw_charging(self) -> bool | None:
        """Return a charging flag that the firmware reports, or ``None`` if the device has none.

        The companion protocol has only a battery voltage. Thus almost all devices answer
        ``None``, and callers use an inference of the charge from the voltage trend instead
        (refer to :meth:`~meshterm.services.battery_service.BatteryService._charging`). A
        transport that can read a real charging flag overrides this method to return it (a
        BLE device with the standard Battery Level Status characteristic, 0x2BED). This
        method is best-effort by contract: it never raises, and it never blocks for a
        significant time. Thus a caller can await it at each poll.
        """
        return None

    @abstractmethod
    async def get_stats(self) -> dict:
        """Return the core, radio, and packet statistics of the firmware, in one dict.

        MeshTerm reads each of the three types of statistics best-effort. Firmware that is
        older than one type gives nothing for it. Thus callers get the subset that exists:

        * core: ``battery_mv``, ``uptime_secs``, ``errors``, ``queue_len``.
        * radio: ``noise_floor``, ``last_rssi``, ``last_snr``, ``tx_air_secs``,
          ``rx_air_secs``.
        * packets: ``recv``, ``sent``, ``flood_tx``, ``direct_tx``, ``flood_rx``,
          ``direct_rx``, ``recv_errors``.
        """

    @abstractmethod
    async def get_path_hash_mode(self) -> int:
        """Return the current path-hash mode (experimental routing flag)."""

    @abstractmethod
    async def get_custom_vars(self) -> dict[str, str]:
        """Return the experimental custom key/value variables of the device."""

    @abstractmethod
    async def get_channel(self, index: int) -> dict | None:
        """Return the settings of one channel, or ``None`` if the slot is not set.

        Args:
            index: The zero-based channel slot.

        Returns:
            A dict with ``channel_idx``, ``channel_name``, and ``channel_secret``
            (16 raw bytes), or ``None`` when the slot is empty.
        """

    async def channel_capacity(self) -> int:
        """Find how many channel slots this device has (read-only, non-destructive).

        The method reads slots from 0 upward until the firmware rejects an index. An empty
        slot is a valid index. It returns ``None``, and the scan does not stop. Only an
        index out of range causes an error answer from the firmware, which comes here as an
        exception. :data:`CHANNEL_SLOT_PROBE_CAP` limits the scan, so a device that never
        rejects an index cannot cause an endless loop. In that case, the method reports the
        cap itself.

        This method only reads the channel settings. Thus it is safe to call it on a live
        device: it does not change the state of the device.

        Returns:
            The number of channel slots that the firmware was built with.
        """
        count = 0
        for idx in range(CHANNEL_SLOT_PROBE_CAP):
            try:
                await self.get_channel(idx)
            except Exception:  # noqa: BLE001 - a rejected index is how firmware signals its ceiling
                break
            count = idx + 1
        return count

    # -- settings: values to set -------------------------------------------------

    @abstractmethod
    async def set_name(self, name: str) -> None:
        """Set the advertised name of the node."""

    @abstractmethod
    async def set_coords(self, lat: float, lon: float) -> None:
        """Set the advertised latitude and longitude of the node (decimal degrees)."""

    @abstractmethod
    async def set_device_pin(self, pin: int) -> None:
        """Set the BLE pairing PIN of the device."""

    @abstractmethod
    async def set_radio(
        self, freq: float, bw: float, sf: int, cr: int, repeat: bool | None = None
    ) -> None:
        """Set the core radio parameters.

        Args:
            freq: The frequency in MHz.
            bw: The bandwidth in kHz.
            sf: The spreading factor.
            cr: The denominator of the coding rate (``5``-``8`` for 4/5-4/8).
            repeat: Whether the companion relays mesh traffic (client repeat, firmware v9+).
                The firmware reads it as an optional last byte of this same command. If the
                byte is absent, the firmware sets the relay to off. Thus a caller that
                changes a radio field on firmware that reports it must state it again. If
                not, the change silently turns off the relay. ``None`` omits the byte, for
                firmware that is older than this byte.
        """

    @abstractmethod
    async def set_tuning(self, rx_delay: float, airtime_factor: float) -> None:
        """Set the radio tuning parameters, in their real units.

        The firmware takes both fields in one command, so a caller that changes one field
        must send the other again. The values are the real values (``rx_delay`` in
        seconds, 0–20, and ``airtime_factor``, a duty-cycle factor, 0–9). The
        implementations apply the ×1000 scaling of the protocol. (The TX delay factors of
        a repeater are not part of this command. Companion firmware reads exactly these
        two fields and ignores all data after them. On repeaters, those values are
        remote-CLI settings.)
        """

    @abstractmethod
    async def set_manual_add_contacts(self, enabled: bool) -> None:
        """Set whether contacts must be added manually instead of automatically."""

    @abstractmethod
    async def set_adv_loc_policy(self, policy: int) -> None:
        """Set the policy that controls whether adverts share the location."""

    @abstractmethod
    async def set_multi_acks(self, value: int) -> None:
        """Set the multi-ack behaviour flag."""

    @abstractmethod
    async def set_telemetry_modes(self, base: int, loc: int, env: int) -> None:
        """Set the three telemetry mode fields together (each ``0``-``3``)."""

    @abstractmethod
    async def set_autoadd_config(self, flags: int, max_hops: int | None = None) -> None:
        """Set the contact auto-add bitmask (:meth:`get_autoadd_config`) and its hop limit.

        The firmware reads the hop limit as an optional second byte. When that byte is
        absent, the firmware does not change the hop limit. Thus ``None`` changes only the
        bitmask.
        """

    @abstractmethod
    async def set_default_flood_scope(self, scope: str) -> None:
        """Store the default flood scope by name (an empty string clears it).

        The name is stored bare, as the firmware and the official apps store it. The ``#``
        is only for the derivation of its 16-byte key (refer to
        :mod:`~meshterm.core.regions`).
        """

    @abstractmethod
    async def set_flood_scope(self, region: str | None) -> None:
        """Set the session send scope: the scope of the next floods, until it changes.

        This is the session override of the firmware (``CMD_SET_FLOOD_SCOPE_KEY``). It is
        not stored, a boot resets it, and it applies to each flood that the companion sends
        (channel messages, flood DMs, acks, path returns, requests). It has priority over
        the stored default. The firmware has no per-channel scope. Thus a channel scope is
        this call immediately before the channel send (and a reset after it).

        Args:
            region: A region name for the scope.
                :data:`~meshterm.core.regions.WILDCARD` (``*``) to force unscoped, also
                over a default scope (firmware 1.16+). Or ``None`` to remove the override
                and use the default scope again.
        """

    @abstractmethod
    async def set_path_hash_mode(self, mode: int) -> None:
        """Set the experimental path-hash routing mode."""

    @abstractmethod
    async def set_custom_var(self, key: str, value: str) -> None:
        """Set an experimental custom variable."""

    @abstractmethod
    async def set_channel(self, index: int, name: str, secret: bytes | None) -> None:
        """Set up a channel slot.

        Args:
            index: The zero-based channel slot.
            name: The channel name (a leading ``#`` derives the secret from the name).
            secret: The 16-byte shared secret, or ``None`` to derive it from ``name``.
        """

    # -- settings: actions ------------------------------------------------------

    @abstractmethod
    async def set_time(self, epoch: int) -> None:
        """Set the device clock to a UNIX epoch timestamp.

        Raises:
            ClockAheadError: If the device clock is already later than ``epoch``. MeshCore
                firmware moves its clock only forward.
        """

    @abstractmethod
    async def send_advert(self, flood: bool = False) -> None:
        """Broadcast an advert (``flood`` sends it across the full mesh)."""

    @abstractmethod
    async def reboot(self) -> None:
        """Reboot the device."""

    @abstractmethod
    async def export_private_key(self) -> str:
        """Export the device's private key as a hex string (sensitive)."""

    @abstractmethod
    async def import_private_key(self, key_hex: str) -> None:
        """Import a private key from a hex string (this replaces the device identity)."""

    @abstractmethod
    async def factory_reset(self) -> None:
        """Erase all the device data and restore the factory defaults (destructive)."""

    async def __aenter__(self) -> Device:
        """Enter the async context manager, and connect the device."""
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the async context manager, and disconnect the device."""
        await self.disconnect()


class MeshCoreDevice(Device):
    """A :class:`Device` on the ``meshcore`` companion client (serial, BLE, or TCP).

    The same wrapper serves all transports. ``transport`` selects the transport that it
    opens:

    * ``"serial"`` opens ``port``.
    * ``"ble"`` opens ``address``.
    * ``"tcp"`` opens ``host``:``tcp_port``.

    All the parts above the connection (commands, event mapping, trace parsing) are the
    same for all transports. Thus only :meth:`connect` and :meth:`link_present` are
    different between them.

    Note:
        The trace mapping here follows the documented companion protocol (trace replies
        have the SNR of each hop, encoded as ``SNR * 4``). But you must check it with your
        firmware version, because the field names of the event payload can change between
        releases.
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
            port: The serial port path (for example ``COM5`` or ``/dev/ttyUSB0``). Only for
                the serial transport.
            baudrate: The serial baud rate.
            connect_timeout: The handshake timeout (seconds) for the first connection. The
                client gets it as its default command timeout. ``None`` uses the default of
                the ``meshcore`` library (approximately 15 s). The startup smoke test sets a
                short value, so that MeshTerm rejects a port that does not respond quickly,
                and does not wait for the full default handshake window.
            transport: ``"serial"`` (default), ``"ble"``, or ``"tcp"``.
            address: The Bluetooth address (for example ``AA:BB:CC:DD:EE:FF``). Only for
                the BLE transport.
            pin: An optional BLE pairing PIN, when the peripheral must have one (BLE only).
            ble_device: The ``bleak.BLEDevice`` that the discovery scan already found at
                ``address``, when available (BLE only). With it, the connect opens the
                peripheral directly, and does not discover it again by address. On Windows,
                a connect with only the address runs a new internal scan. That scan
                sometimes does not find a companion that advertises slowly, and this is the
                main cause of BLE startups that fail at random. The type is ``object``, so
                the paths without BLE do not have to import ``bleak``.
            host: The host name or IP address of a network companion. Only for the TCP
                transport.
            tcp_port: The TCP port on which the network companion listens. Only for the
                TCP transport.
        """
        self._port = port
        self._baudrate = baudrate
        self._connect_timeout = connect_timeout
        self._transport = transport
        self._address = address
        self._pin = pin
        #: The result of the last Windows PIN pairing (``None`` when no pairing ran). With
        #: it, an authentication refusal can name the failure, instead of a guess.
        self._ble_pairing: _BlePairing | None = None
        self._ble_device = ble_device
        self._host = host
        self._tcp_port = tcp_port
        self._mc = None  # type: ignore[var-annotated]  # meshcore.MeshCore
        #: The ``bleak.BleakClient`` of a BLE link. MeshTerm keeps it because ``meshcore``
        #: releases its own reference at the moment the peripheral disappears. Refer to
        #: :meth:`_release_ble_client` for why nothing else can close it, and for the cost
        #: when it stays open.
        self._ble_client = None  # type: ignore[var-annotated]  # bleak.BleakClient
        #: The BlueZ agent that answers the pairing of this device while a connect runs
        #: (Linux, refer to :meth:`_ble_pairing_agent`). With it, the connect can tell a
        #: pairing refusal from a lost link.
        self._ble_agent: object | None = None
        # This lock serializes the channel reads. The get_channel of the meshcore library
        # waits for "the next CHANNEL_INFO event", with no correlation to the index that it
        # asked for. The dispatcher sends that event to each waiter that is in progress.
        # Thus two concurrent reads both complete on the first response, and one caller
        # silently gets the channel of the other caller. This lock keeps a maximum of one
        # channel read in progress, so the response is certainly ours.
        self._channel_read_lock = asyncio.Lock()
        #: Set after we log a device that has the standard BLE Battery Service. Thus the
        #: "using its charging flag" log occurs one time in each session, not at each poll.
        self._logged_bas = False

    @property
    def transport(self) -> str:
        """The transport of this device (``"serial"``, ``"ble"``, or ``"tcp"``)."""
        return self._transport

    @property
    def endpoint(self) -> str | None:
        """The connection endpoint: ``host:port`` for TCP, the BLE address, or else the port."""
        if self._transport == "tcp":
            return f"{self._host}:{self._tcp_port}"
        return self._address if self._transport == "ble" else self._port

    async def connect(self) -> None:  # noqa: D102 - inherited docstring
        if self._mc is not None:
            return
        try:
            from meshcore import MeshCore  # lazy import: --mock has no hardware dependencies
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "The 'meshcore' library is required to talk to real hardware but is not "
                "installed. Run `pip install -e .` (or `pip install meshcore`), or use "
                "--mock for the simulator."
            ) from exc

        # Each transport leaves here with a sentence. A failure that it recognizes gets a name
        # and its repair. A failure that it does not recognize still gets words: what MeshTerm
        # tried to open, and the message of the error, with the traceback in the log (refer
        # to _unrecognised). In the past, a bare library error went out instead, and a dialog
        # could only call it "didn't answer". The original error stays the cause, so
        # is_connection_lost still finds it.
        if self._transport == "ble":
            where = self._address or "the selected Bluetooth device"
            try:
                self._mc = await self._create_ble(MeshCore)
            except DeviceCommandError:
                raise
            except (TimeoutError, asyncio.TimeoutError) as exc:
                _log.warning("BLE connect to %s timed out", where)
                raise DeviceCommandError(_ble_stalled_message(where)) from exc
            except Exception as exc:  # noqa: BLE001 - said in words, logged whole
                raise _unrecognised(f"couldn't connect to {where} over Bluetooth", exc) from exc
        elif self._transport == "tcp":
            try:
                self._mc = await MeshCore.create_tcp(
                    self._host, self._tcp_port, default_timeout=self._connect_timeout
                )
            except Exception as exc:  # noqa: BLE001 - named when recognised, else said in words
                # The socket did not open. This is not a failure at the MeshCore level. The
                # name did not resolve, nothing listens on the port, no route goes to the
                # host, or the connect timed out. These are four faults with four different
                # repairs.
                message = _tcp_open_message(self._host or "the host", self._tcp_port, exc)
                if message is None:
                    raise _unrecognised(f"couldn't connect to {self.endpoint}", exc) from exc
                _log.warning("couldn't connect to %s: %s", self.endpoint, exc)
                raise DeviceCommandError(message) from exc
        else:
            try:
                self._mc = await MeshCore.create_serial(
                    self._port, self._baudrate, default_timeout=self._connect_timeout
                )
            except Exception as exc:  # noqa: BLE001 - named when recognised, else said in words
                # The port did not open at all. This tells nothing about whether a companion
                # is on it, but in the past MeshTerm reported it as if it told exactly that.
                port = self._port or "the serial port"
                message = _serial_open_message(port, exc)
                if message is None:
                    raise _unrecognised(f"couldn't connect to the device on {port}", exc) from exc
                _log.warning("couldn't open %s: %s", self._port, exc)
                raise DeviceCommandError(message) from exc
        # ``create_*`` returns ``None`` (after it cleans up its own connection) when the node
        # never answers the identity handshake. That is, the endpoint is not a MeshCore
        # companion. Show that as a clean error that MeshTerm can recover from. Do not leave
        # a half-open device, whose next command fails with a confusing "not connected".
        if self._mc is None:
            raise DeviceCommandError(self._no_response_message())
        # A short ``connect_timeout`` is only a limit for the first identity handshake (so
        # that MeshTerm rejects a dead endpoint quickly). Now we are connected, so restore
        # the usual command timeout of the library. Then the rest of the session has the
        # usual time for each command.
        if self._connect_timeout is not None:
            commands = getattr(self._mc, "commands", None)
            default = getattr(commands, "DEFAULT_TIMEOUT", None)
            if commands is not None and default is not None:
                commands.default_timeout = default

    async def _create_ble(self, mesh_core):  # type: ignore[no-untyped-def]
        """Open the BLE companion connection, and on Windows, pair with a PIN first.

        MeshTerm does not set ``auto_reconnect``, on purpose. MeshTerm controls the
        reconnect itself (with the same reconnect dialog that the serial path uses). Thus
        the meshcore client must report a lost link quickly through ``is_connected``, and
        must not silently try again below us.

        Before MeshTerm opens the link, it makes an *authenticated* pairing itself when the
        user gives a PIN (refer to :meth:`_pair_ble_windows`). This is essential on Windows.
        bleak's ``pair()`` does only the "Just Works" ceremony (``CONFIRM_ONLY``), and it
        never enters a passkey. Thus a companion with a PIN makes a bond without
        authentication, and then rejects the GATT subscribe on its authenticated UART
        characteristic. MeshTerm then reports a correct PIN as "rejected", and the device
        can never connect.

        When MeshTerm runs the WinRT ProvidePin ceremony itself, it makes the authenticated
        bond that is necessary for the characteristic. After the bond, the OS keeps it, and
        later reconnects do not ask for a PIN. This step does nothing (and causes no
        problem) on other platforms, when no PIN is set, or when the device already has a
        bond.

        Args:
            mesh_core: The imported ``meshcore.MeshCore`` class.

        Returns:
            The connected ``MeshCore`` client, or ``None`` if the peripheral never answered.
        """
        async with self._ble_pairing_agent() as agent:
            self._ble_agent = agent
            try:
                await self._pair_ble(force=False)
                return await self._open_ble(mesh_core, allow_repair=True)
            finally:
                self._ble_agent = None

    def _pin_was_asked(self) -> bool:
        """Whether BlueZ asked the agent of this connect for the PIN.

        Refer to :class:`bluez.PinAgent`.
        """
        return bool(getattr(self._ble_agent, "asked", False))

    def _ble_pairing_agent(self) -> AbstractAsyncContextManager[object]:
        """Answer the BlueZ pairing requests for this device during a connect (Linux only).

        Refer to :func:`meshterm.core.bluez.answering` for the reason. BlueZ starts its own
        pairing when the subscribe is refused. If nothing answers it, the connect stops for
        30 s. This is an instance hook, so tests can keep the system bus out of it.
        """
        from . import bluez

        if sys.platform.startswith("linux") and bluez.is_mac(self._address or ""):
            return bluez.answering(self._address or "", self._pin)
        return nullcontext()

    async def _open_ble(self, mesh_core, *, allow_repair: bool):  # type: ignore[no-untyped-def]
        """Open the meshcore BLE client, translate auth failures, and repair old bonds.

        Args:
            mesh_core: The imported ``meshcore.MeshCore`` class.
            allow_repair: Whether a GATT authentication failure can start one retry that
                unpairs and then pairs again with the PIN (Windows only). It is ``False`` on
                that retry, so that a PIN that is in fact wrong cannot cause a loop.

        Returns:
            The connected ``MeshCore`` client, or ``None`` if the peripheral never answered.

        Raises:
            DeviceCommandError: If ``bleak`` is missing (with install instructions).
            DeviceAuthenticationError: If the companion must have a pairing PIN that we do
                not have, or that it rejected.
        """
        try:
            return await self._create_ble_with_retry(mesh_core)
        except ImportError as exc:
            raise DeviceCommandError(
                "Bluetooth support requires the 'bleak' package, which isn't installed. "
                "Run `pip install -e .` (or `pip install bleak`), use a USB device, or "
                "run with --mock."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - translate auth failures. Raise the others again.
            # A companion with a PIN accepts the link-layer connection, but rejects the GATT
            # subscribe with an authentication error ("Insufficient Authentication",
            # "Insufficient Encryption", or "not paired"). That is not a lost link. It is a
            # missing bond. Thus show a clean message that the user can act on, instead of a
            # raw traceback (a bare BleakGATTProtocolError would give a raw traceback). All
            # other errors go up with no change, so a real link loss still goes to
            # is_connection_lost.
            if not _is_ble_auth_error(exc):
                raise
            # On Windows, this can also occur with the right PIN, when an old unauthenticated
            # "Just Works" bond from an earlier try is in the way. is_paired is true, so
            # MeshTerm skipped the ProvidePin step above, but the bond cannot unlock the
            # characteristic. Clear the bond, pair with the PIN, and try the connect one more
            # time before a failure.
            if allow_repair and await self._pair_ble(force=True):
                return await self._open_ble(mesh_core, allow_repair=False)
            # On macOS, the same rejection has the opposite meaning. It is not the end of a
            # pairing try, but the start of one, because an access to the authenticated
            # characteristic is the only way to make CoreBluetooth pair. The OS opens its
            # Passkey dialog while we unwind. Wait for the user to answer it.
            if allow_repair and sys.platform == "darwin":
                return await self._open_ble_after_macos_pairing(mesh_core, exc)
            raise await self._ble_auth_failure() from exc

    async def _open_ble_after_macos_pairing(self, mesh_core, cause: BaseException):  # type: ignore[no-untyped-def]
        """Open the link again while macOS runs the Passkey dialog that it just opened.

        CoreBluetooth has no pairing API. In Apple's model, a peripheral pairs *implicitly*
        when something accesses a characteristic that demands encryption. The companion
        firmware sets its UART characteristic to ENC+MITM to cause exactly this
        (``SECMODE_ENC_WITH_MITM`` on nRF52, ``ESP_GATT_PERM_*_ENC_MITM`` on ESP32). Thus
        the subscribe fails with "Insufficient Authentication", and that failure is what
        makes macOS ask for the code. The GATT operation has already failed at that time.
        The bond that it started arrives some seconds later, after a person has typed six
        digits.

        When MeshTerm failed at that point, it reported an error for a pairing that was in
        fact successful. The device then connected on the next try, because the first try
        had silently done the work. Thus try again instead of a failure, for a time that is
        sufficient for the typing.

        Args:
            mesh_core: The imported ``meshcore.MeshCore`` class.
            cause: The authentication failure that opened the dialog. It is chained onto
                the final error if the pairing never completes.

        Returns:
            The connected ``MeshCore`` client, or ``None`` if a later try opened the
            transport but the peripheral never answered the identity handshake.

        Raises:
            DeviceAuthenticationError: If the companion still refused each try: the user
                closed the dialog, or typed a wrong code.
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
        """Open the owned BLE client, and retry the transport failures that are temporary.

        The meshcore client raises a bare ``ConnectionError`` when it could not open the
        *link itself*. For example, bleak's internal lookup did not find the peripheral, or
        the link-layer connect timed out. On Windows, both failures are usually temporary.
        One scan window easily misses a companion that advertises at a slow interval, and a
        connect immediately after the discovery scan can race the radio. Users learned to
        select the device again, which is only a manual retry. Thus MeshTerm tries again
        here, for a short time, before it reports the failure.

        The ``BLEDevice`` that the scan of the picker already found (if it found one) goes
        to the client. Thus the client connects to it directly, and does not discover the
        address again.

        A link that opened and then was lost before the session started (refer to
        :func:`_is_ble_link_drop`) gets a retry in the same way: the cause is the radio, not
        a refusal of the device. No other failure gets a retry. A PIN or bond rejection, or
        any other GATT failure, goes up with no change on the first try. Thus the
        authentication code in :meth:`_open_ble` (and a PIN that is in fact wrong) never makes a
        loop. Each try makes its own client through :meth:`_connect_owned_ble`, which
        closes the client before it lets a failure out. Thus a retry always starts from a
        released link, never from a leaked link.

        Args:
            mesh_core: The imported ``meshcore.MeshCore`` class.

        Returns:
            The connected ``MeshCore`` client, or ``None`` if the transport connected but
            the peripheral never answered the identity handshake.

        Raises:
            DeviceCommandError: If no try could open the link. The message says this,
                because this is the only failure where MeshTerm never reached the device at
                all. The message "didn't answer as a MeshCore device" sent users to examine
                the firmware.
        """
        last_exc: Exception | None = None
        for attempt in range(_BLE_CONNECT_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_BLE_CONNECT_RETRY_DELAY_S)
            try:
                return await self._connect_owned_ble(mesh_core)
            except Exception as exc:  # noqa: BLE001 - only a link failure is kept and retried
                if not (isinstance(exc, ConnectionError) or _is_ble_link_drop(exc)):
                    raise
                _log.debug(
                    "BLE link to %s failed to open (attempt %d/%d): %s",
                    self._address,
                    attempt + 1,
                    _BLE_CONNECT_ATTEMPTS,
                    exc,
                )
                last_exc = exc
        assert last_exc is not None  # the loop always runs, and only a link failure gets here
        where = self._address or "the selected Bluetooth device"
        _log.warning("BLE link to %s never opened: %s", where, last_exc)
        raise DeviceCommandError(
            f"couldn't open a Bluetooth link to {where} ({_BLE_CONNECT_ATTEMPTS} tries) — it "
            "wasn't found, didn't accept the connection, or dropped it while connecting. It "
            "may be out of range, powered off, or connected to a phone or another computer (a "
            "companion takes one connection at a time)."
        ) from last_exc

    async def _connect_owned_ble(self, mesh_core):  # type: ignore[no-untyped-def]
        """Make the meshcore BLE client here and connect it, so that we own its teardown.

        ``MeshCore.create_ble`` makes a client, calls ``connect()`` on it, and returns it
        *only on success*. Thus a connect that **raises** leaves that client, and the bleak
        link that it has already opened, inside the library with no owner and with no
        reference that we can close. This is not only a theory. A companion with a PIN
        answers the notify subscribe without a bond with a GATT authentication error.
        ``connect()`` raises this error from deep inside, after bleak has opened the link.

        Our own :meth:`disconnect` then does nothing (``_mc`` was never set), Windows keeps
        the ACL link for the life of the process, and the peripheral (which still thinks
        that it has a peer) **stops advertising**. Thus the PIN dialog that opens next asks
        for a code that it can no longer deliver. The retry cannot find the device, and the
        user sees that the companion refuses a correct PIN. When we make the same two
        objects here, it costs three lines and keeps the handle. Thus each exit closes the
        link.

        This method covers both public failure forms. For the one form that ``create_ble``
        handles, this method does the same as ``create_ble``. The two forms are: a raise
        (closed, then raised again), and a ``None`` from the identity handshake (closed,
        then reported as "not a companion").

        Args:
            mesh_core: The imported ``meshcore.MeshCore`` class.

        Returns:
            The connected ``MeshCore`` client, or ``None`` if the transport opened but the
            peripheral never answered the identity handshake.

        Raises:
            Exception: The exception that ``connect`` raised, but only after the link is
                closed.
        """
        from meshcore import BLEConnection

        # bleak never gets the PIN, on any platform.
        #
        # macOS: if bleak gets a PIN, ``BLEConnection.connect`` calls bleak's
        # ``client.pair()``. CoreBluetooth has no pairing API at all, so the macOS backend
        # raises ``NotImplementedError`` immediately. The library then disconnects and
        # raises the error again. Thus a *correct* PIN is what breaks the connection. In
        # Apple's model, the OS runs the pairing, not the app. The companion firmware sets
        # its UART characteristic to ENC+MITM (``SECMODE_ENC_WITH_MITM`` on nRF52,
        # ``ESP_GATT_PERM_*_ENC_MITM`` on ESP32). Thus the companion answers the subscribe
        # below (without a bond) with "Insufficient Authentication", and macOS then runs
        # Passkey Entry and asks for the code itself. Thus, when we give no PIN here, a
        # companion with a PIN can make a bond. The OS keeps the bond, and later
        # connections do not ask for a PIN. (A firmware that guards only the write also
        # gets there, through :func:`_record_write_refusals`.)
        #
        # Linux: the same result from the other side. bleak's BlueZ ``pair()`` ignores the
        # PIN, and pairs through the system agent that is available (a desktop dialog, or
        # nothing at all). Thus MeshTerm pairs there itself before the connect (refer to
        # :meth:`_pair_ble_bluez`). If bleak got the PIN, it would only start a second
        # pairing that we cannot answer.
        #
        # Windows: bleak's WinRT ``pair()`` always uses CONFIRM_ONLY ("Just Works"), and it
        # never sends a PIN. After a failed PIN pairing of our own (a PIN with a typing
        # error), we gave bleak the PIN, and bleak made a bond *without* a PIN. Windows kept
        # that unauthenticated bond. The next try, with the right PIN, then used that bond
        # again and was refused, until a person removed the bond manually.
        #
        # We run the pairing on each platform that has an API for it (refer to
        # :meth:`_pair_ble`).
        connection = BLEConnection(address=self._address, device=self._ble_device, pin=None)
        seen = _hold_disconnects_while_connecting(connection, self._pin_was_asked)
        refused = _record_write_refusals(connection)
        mc = mesh_core(
            connection,
            default_timeout=self._connect_timeout,
            auto_reconnect=False,
        )
        try:
            started = await _unless_hung_up(mc.connect(), seen.hung_up)
        except BaseException:
            self._ble_client = _held_client(connection, seen)
            await MeshCoreDevice._discard_meshcore(mc)
            await self._release_ble_client()
            raise
        finally:
            seen.connecting = False
        # Take our own reference to the bleak client now, while ``BLEConnection`` still has
        # one. ``BLEConnection`` releases its reference at the moment the peripheral goes
        # away. After that, nothing else can reach the object that MeshTerm must close
        # (refer to :meth:`_release_ble_client`).
        self._ble_client = _held_client(connection, seen)
        if started is None:
            await MeshCoreDevice._discard_meshcore(mc)
            await self._release_ble_client()
            # An empty handshake with a write that was refused because there was no bond is
            # a pairing problem, not an unknown device on the air (refer to
            # :func:`_record_write_refusals`).
            refusal = refused.refusal()
            if refusal is not None:
                raise refusal
            return None
        return mc

    @staticmethod
    async def _discard_meshcore(mc) -> None:  # type: ignore[no-untyped-def]
        """Close a client whose ``connect`` did not complete, with a time limit, and never raise.

        The graceful ``mc.disconnect()`` is **not sufficient alone here**, and that is the
        main difficulty of this path. ``ConnectionManager.disconnect`` closes the transport
        only ``if self._is_connected``. It sets this flag *after* ``connection.connect()``
        returns. A connect that raised (the GATT authentication error, raised from
        ``start_notify`` well after bleak opened the link) never got that far. Thus the
        manager is certain that there is nothing to close, while ``BLEConnection`` still
        holds a live, connected ``BleakClient``. A call to only the graceful path seems to
        be a teardown, but the link leaks. That is exactly why the first try at this repair
        still left the peripheral off the air.

        Thus both parts run. First the graceful call (it stops the dispatcher and cancels
        any reconnect task), then a direct close of the transport, which is what in fact
        closes the link. ``BLEConnection.disconnect`` checks ``client.is_connected`` again,
        so the second close does nothing when the first one did the work.

        The close is shielded on purpose. The other way into this method is a probe whose
        ``wait_for`` expired and cancelled the handshake while it ran. A plain ``await``
        would then be cancelled itself at the moment it suspended. It would stop the
        teardown that it was called to do, and leak exactly the link that this method must
        close. The shield lets the close complete on its own, while the cancellation
        continues to go up to our caller.

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
            # This includes CancelledError. MeshTerm ignores it here only so that the
            # exception (or the cancellation) of the caller is the one that goes up.
            # ``closing`` continues to run in all cases.
            _log.debug("discarding a half-open BLE client: %s", exc)

    @staticmethod
    async def _force_close_transport(mc) -> None:  # type: ignore[no-untyped-def]
        """Cancel the dispatcher and close the raw transport directly. Best-effort, silent.

        This is the way out of both library traps: a dispatcher stop that deadlocks on its
        own ``queue.join()``, and a connection manager that does not close a transport that
        it never marked as connected. This method goes past both, and that is what in fact
        releases the serial port or the BLE link.

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

    async def _pair_ble(self, *, force: bool) -> bool:
        """Pair with the PIN through the OS, where MeshTerm must do it (Windows, Linux).

        This is the one entry for :meth:`_create_ble` and its repair of an old bond. Thus
        the connect path is the same on all platforms, and only the ceremony is different.
        macOS has no pairing API, and pairs on its own (refer to
        :meth:`_open_ble_after_macos_pairing`).

        Args:
            force: Remove a bond that exists, and pair again from the start (refer to
                :meth:`_pair_ble_windows`).

        Returns:
            ``True`` if an authenticated bond exists after the call.
        """
        if not self._pin:
            return False
        if sys.platform == "win32":
            return await self._pair_ble_windows(force=force)
        if sys.platform.startswith("linux"):
            return await self._pair_ble_bluez(force=force)
        return False

    async def _pair_ble_bluez(self, *, force: bool) -> bool:
        """Pair through BlueZ, with our own agent that answers the PIN (Linux only).

        Refer to :mod:`meshterm.core.bluez` for why bleak cannot do it: BlueZ asks a pairing
        *agent* for the passkey, and bleak registers no agent. This method stores its result
        in ``_ble_pairing``, as the Windows ceremony does, so a refusal can tell which step
        failed.

        Args:
            force: Remove a bond that exists first. This repairs a bond that the device has
                lost.

        Returns:
            ``True`` if an authenticated bond exists after the call.
        """
        from . import bluez

        if not self._pin or not bluez.is_mac(self._address or ""):
            return False
        outcome, status = await bluez.pair(self._address or "", self._pin, force=force)
        self._ble_pairing = _BlePairing(outcome, status)
        if outcome in ("failed", "error"):
            _log.warning("BLE PIN pairing with %s failed: %s", self._address, status)
        else:
            _log.debug("BLE PIN pairing with %s: %s", self._address, outcome)
        return outcome in ("paired", "reused")

    async def _pair_ble_windows(self, *, force: bool) -> bool:
        """Make an authenticated BLE bond through the WinRT ProvidePin ceremony (Windows only).

        This is the only place where MeshTerm in fact gives a Bluetooth passkey to the
        peripheral. bleak's own ``pair()`` on Windows always uses the ``CONFIRM_ONLY``
        ("Just Works") ceremony, and never sends a PIN. Thus bleak cannot make a bond with a
        companion that demands passkey pairing. The authenticated UART characteristic of
        that companion continues to reject the notify subscribe with *Insufficient
        Authentication*. Instead, we run the ``PROVIDE_PIN`` ceremony directly on WinRT (the
        same ceremony that the Windows "Add device" dialog uses), and give it :attr:`_pin`.
        This gives the ``ENCRYPTION_AND_AUTHENTICATION`` bond that is necessary for the
        characteristic. Windows stores the bond, so later sessions reconnect with no PIN.

        This method is best-effort and self-contained. It returns a bool and does not
        raise. It catches all errors (winrt projection absent, device out of range, an API
        problem), so the caller continues to the usual connect. The auth-error translation
        of that connect still gives the right message. The method does nothing (and returns
        ``False``) on other OSes than Windows, when no PIN is set, or when the address is
        not a MAC that it can parse.

        Args:
            force: When ``False``, the method trusts a bond that exists and uses it again
                (the fast path). When ``True``, it first removes any bond that exists, and
                makes the bond again with the PIN. This repairs an old, unauthenticated
                "Just Works" bond that a plain reconnect cannot repair.

        Returns:
            ``True`` if an authenticated bond exists after the call (a new bond, or a bond
            that was already there), ``False`` in other cases.
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
        except Exception as exc:  # noqa: BLE001 - no winrt projection. Continue without it.
            _log.debug("BLE PIN pairing unavailable (winrt import failed): %s", exc)
            return False

        # Each exit stores its result in ``_ble_pairing``, so a later refusal can tell which
        # of these it was. The bool alone is sufficient only to decide about a retry.
        device = None
        try:
            device = await BluetoothLEDevice.from_bluetooth_address_async(address)
            if device is None:
                self._ble_pairing = _BlePairing("absent")
                return False  # out of range, or it cannot connect now
            pairing = device.device_information.pairing
            if pairing.is_paired:
                if not force:
                    self._ble_pairing = _BlePairing("reused")
                    return True  # trust the (authenticated) bond that exists: the fast path
                # Remove the old bond, then get the device again. The pairing object is a
                # snapshot and does not show the unpair, so a new device_information is
                # necessary to pair again.
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
                # The peripheral asked for a passkey. Give it the passkey that we got.
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
                # A warning, not debug: this line tells a wrong PIN from a device that refused
                # to pair, and the default log level would hide a debug line.
                self._ble_pairing = _BlePairing("failed", name)
                _log.warning("BLE PIN pairing with %s failed: %s", self._address, name)
            return ok
        except Exception as exc:  # noqa: BLE001 - best-effort. The caller continues on False.
            self._ble_pairing = _BlePairing("error", str(exc))
            _log.warning("BLE PIN pairing with %s raised: %s", self._address, exc)
            return False
        finally:
            MeshCoreDevice._close_ble_device(device)

    @staticmethod
    def _ble_address_int(address: str) -> int | None:
        """Parse an ``AA:BB:CC:DD:EE:FF`` MAC (or one with dashes) into the ulong that WinRT uses.

        Args:
            address: The Bluetooth address string that bleak reported for the device.

        Returns:
            The 48-bit address as an int, or ``None`` if it is not a MAC of 12 hex digits
            (for example, a CoreBluetooth UUID on macOS, where this pairing path does not
            apply in any case).
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
        """Release a WinRT ``BluetoothLEDevice`` handle, and close the OS link to the peripheral.

        Each ``BluetoothLEDevice.from_bluetooth_address_async`` returns an ``IClosable``.
        While this object exists, it keeps the ACL connection of the operating system to the
        radio open. An unpair removes the *bond*, but it never closes that *link*. The link
        closes only when the last handle to it closes. If we leak the handle, Windows reports
        the device as still "connected" (but unpaired), and the peripheral never gets a clean
        disconnect. The peripheral then refuses to pair again until a power cycle. Thus each
        helper that opens one of these handles must close it, also on the error paths.

        This method is best-effort and silent: a missing ``close`` projection or a second
        close is not important enough to report during the teardown.
        """
        try:
            if device is not None:
                device.close()
        except Exception as exc:  # noqa: BLE001 - releasing a handle must never raise
            _log.debug("BLE device handle close failed: %s", exc)

    @staticmethod
    async def is_ble_paired(address: str) -> bool:
        """Whether the OS has a bond for the BLE peripheral at ``address`` (Windows, Linux).

        This is the read-only partner of :meth:`_pair_ble_windows` and :meth:`unpair_ble`.
        It asks WinRT if an OS-level pairing exists. Thus the UI can decide if an "unpair"
        action has a use (a device with a bond and a PIN) or not (an open companion that
        never made a bond, or a serial link). It shows the *OS bond*, not the record that
        MeshTerm remembers (the two are independent). It stays true across sessions, also
        when this run gave no PIN, because Windows stores the bond.

        This method is best-effort and self-contained. It returns ``False`` (and does not
        raise) on other OSes than Windows, when the winrt projection is not available, when
        the address is not a MAC that it can parse, or on any WinRT problem. Thus a caller
        can treat it as a plain "is there anything to unpair?".

        Args:
            address: The Bluetooth MAC (``AA:BB:CC:DD:EE:FF``, or with dashes) to query.

        Returns:
            ``True`` only when Windows reports a live bond for the device.
        """
        if sys.platform.startswith("linux"):
            from . import bluez

            return bluez.is_mac(address or "") and await bluez.is_paired(address)
        if sys.platform != "win32":
            return False
        addr = MeshCoreDevice._ble_address_int(address or "")
        if addr is None:
            return False
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEDevice
        except Exception as exc:  # noqa: BLE001 - no winrt projection, so nothing to unpair
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
        """Remove the OS-level bond for the BLE peripheral at ``address`` (Windows and Linux).

        This is the inverse of :meth:`_pair_ble_windows`. It removes the stored
        ``ENCRYPTION_AND_AUTHENTICATION`` bond, so the next connection must run the PIN
        ceremony again from the start. It is the "forget this pairing" primitive of
        *Unpair & quit* in the quit dialog. It changes only the OS bond, never the device
        record that MeshTerm remembers. MeshTerm keeps that record on purpose (the device
        keeps its friendly name and stays in the picker, and it only asks for its PIN again
        next time).

        Call it only *after* the teardown of the companion link, because you cannot cleanly
        remove a bond that an open connection still uses. This method is best-effort and
        self-contained. It returns a bool and does not raise. It does nothing (and returns
        ``False``) on other OSes than Windows, when winrt is not available, when the address
        is not a MAC, or when there is no bond to remove.

        Args:
            address: The Bluetooth MAC (``AA:BB:CC:DD:EE:FF``, or with dashes) to unpair.

        Returns:
            ``True`` if a bond was removed. ``False`` if there was nothing to unpair, or if
            the try failed.
        """
        if sys.platform.startswith("linux"):
            from . import bluez

            return bluez.is_mac(address or "") and await bluez.unpair(address)
        if sys.platform != "win32":
            return False
        addr = MeshCoreDevice._ble_address_int(address or "")
        if addr is None:
            return False
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEDevice
            from winrt.windows.devices.enumeration import DeviceUnpairingResultStatus
        except Exception as exc:  # noqa: BLE001 - no winrt projection. Continue without it.
            _log.debug("BLE unpair unavailable (winrt import failed): %s", exc)
            return False
        device = None
        try:
            device = await BluetoothLEDevice.from_bluetooth_address_async(addr)
            if device is None:
                return False
            pairing = device.device_information.pairing
            if not pairing.is_paired:
                return False  # no bond: treat this as a no-op that reached its goal
            result = await pairing.unpair_async()
            status = int(result.status)
            ok = status == int(DeviceUnpairingResultStatus.UNPAIRED)
            _log.debug("BLE unpair for %s: status=%d ok=%s", address, status, ok)
            return ok
        except Exception as exc:  # noqa: BLE001 - best-effort teardown. Never crash the exit.
            _log.debug("BLE unpair attempt failed for %s: %s", address, exc)
            return False
        finally:
            # The close of the handle is what in fact closes the OS link to the peripheral.
            # Without it, the device stays "connected" after the unpair, and it does not pair
            # again until it reboots.
            MeshCoreDevice._close_ble_device(device)

    async def _ble_os_bonded(self) -> bool:
        """Whether the OS has a bond for this device (asked only to explain a refusal).

        This is an instance hook over :meth:`is_ble_paired`, so tests can answer it without
        the OS.
        """
        return await MeshCoreDevice.is_ble_paired(self._address or "")

    async def _ble_auth_failure(self) -> DeviceAuthenticationError:
        """Tell *which* authentication failure this was, and what repairs it.

        The GATT refusal is the same on the link for all causes, but the causes do not have
        the same repair. One sentence for all of them sent users to the wrong repair. For
        example, MeshTerm reported an old Windows bond as "requires a PIN" to a user whose
        device Windows clearly showed as paired. Thus MeshTerm makes the error from the facts
        that it knows:

        * macOS: the OS runs the pairing in its own dialog. Say this.
        * No PIN, and the OS has a bond: the bond is old (after the bond was made, the
          device was flashed again, reset, or got a new PIN), and a new pairing is the
          repair.
        * No PIN, no bond: the user must give the PIN of the device.
        * A PIN, and MeshTerm's own pairing (WinRT on Windows, a BlueZ agent on Linux)
          reported a status: a wrong PIN, a device that refused to pair, or some other
          status. The message names each one.
        * A PIN, the pairing was successful, and the device still refused: the device has
          an old bond for this computer.
        * A PIN, and nothing more is known: the PIN was rejected.

        The message names the OS (Windows, Linux), because each OS has its own place to
        remove a bond, and that is half of most of these repairs. MeshTerm also logs each
        case, so a log from another machine tells which case it was.

        Returns:
            The error to raise. It has a one-line :attr:`~DeviceAuthenticationError.hint`
            for the PIN dialog.
        """
        where = self._address or "the selected Bluetooth device"
        # The OS bond changes the message in only one case, so MeshTerm asks for it only there.
        bonded = _ble_host() is not None and not self._pin and await self._ble_os_bonded()
        message, hint = self._ble_auth_diagnosis(where, bonded)
        _log.warning("BLE connect to %s refused: %s", where, message)
        return DeviceAuthenticationError(message, hint=hint)

    def _ble_auth_diagnosis(self, where: str, os_bonded: bool) -> tuple[str, str]:
        """The message and the dialog hint for :meth:`_ble_auth_failure` (it lists the cases).

        Args:
            where: The device, as the message names it.
            os_bonded: Whether the OS has a bond (asked only when no PIN was given).

        Returns:
            ``(message, hint)``.
        """
        if sys.platform == "darwin":
            # macOS gets the code itself, in its own dialog. Thus --ble-pin is not the repair
            # here, and if the message named it, it would send the user to a place that
            # cannot help.
            return (
                f"{where} was not paired. macOS asks for the pairing code in its own dialog "
                "rather than through MeshTerm — enter the 6-digit code shown on the device "
                "(or in the MeshCore app) when it appears, and the bond is remembered for "
                "next time. If no dialog appeared, check that Bluetooth is allowed for this "
                "terminal in System Settings > Privacy & Security > Bluetooth.",
                "",
            )
        host = _ble_host() or "The system"
        forget = _ble_forget(where)
        if not self._pin:
            if os_bonded:
                return (
                    f"{host} has {where} paired, but the device refused that pairing — it is "
                    "probably out of date (the device was reflashed, reset, or given a new PIN "
                    f"since). Pass --ble-pin <PIN> and MeshTerm will pair it again, or {forget} "
                    "and reconnect.",
                    f"{host}'s saved pairing was refused — the PIN pairs it again.",
                )
            once = (
                " On Windows you may also need to pair the device once in Settings > Bluetooth "
                "before it will connect."
                if sys.platform == "win32"
                else ""
            )
            return (
                f"{where} requires a Bluetooth pairing PIN. Pass it with --ble-pin <PIN> (the "
                f"6-digit code shown on the device or in the MeshCore app).{once}",
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
                f"{host} couldn't pair with {where} ({status}). Restart the device, {forget}, "
                "and try again.",
                f"{host} couldn't pair ({status}).",
            )
        if outcome == "absent":
            return (
                f"{host} couldn't reach {where} to pair it — it may be out of range, asleep, "
                "or connected to another device.",
                f"{host} couldn't reach the device to pair it.",
            )
        if outcome == "error":
            return (
                f"{host}'s pairing call failed for {where}: {status}",
                f"{host}'s pairing call failed — the log has the detail.",
            )
        if outcome in ("paired", "reused"):
            return (
                f"{host} paired with {where}, but the device still refuses the connection — "
                "it is probably holding an old pairing for this computer. Restart the device, "
                f"{forget}, and try again.",
                "Paired, but still refused — restart the device and retry.",
            )
        return (
            f"{where} rejected the Bluetooth PIN — it needs pairing and the PIN provided "
            "wasn't accepted. Double-check the 6-digit code shown on the device (or in the "
            "MeshCore app) and pass it with --ble-pin, then try again.",
            "That PIN was rejected — check the code and try again.",
        )

    def _no_response_message(self) -> str:
        """A clean, recoverable error for an endpoint that did not answer as a companion."""
        if self._transport == "ble":
            # The link opened (a link that did not open has its own message). Thus range and
            # power are not the problem here. The problem is what answered.
            where = self._address or "the selected Bluetooth device"
            return (
                f"connected to {where} over Bluetooth, but it never answered as a MeshCore "
                "companion — it may be running repeater or room server firmware, or be busy "
                "with another app."
            )
        if self._transport == "tcp":
            # The socket opened (a socket that did not open has its own message). Thus the
            # address and the network are not the problem here. The problem is what listens
            # on the port.
            where = self.endpoint or "the selected network device"
            return (
                f"the connection to {where} opened, but nothing answered as a MeshCore "
                "companion — the port may belong to another service, or the companion may "
                "be busy with another client."
            )
        # ModemManager opens the port without a lock. Thus the port opens correctly, and
        # ModemManager breaks the handshake instead. That is why this message must name it,
        # not the error message of the open.
        modem = (
            " On Linux, ModemManager may be probing it: `sudo systemctl stop ModemManager` "
            "and try again."
            if sys.platform.startswith("linux")
            else ""
        )
        return (
            f"no response from a MeshCore companion on {self._port}; it may not be a "
            f"MeshCore device, or it may be powered off or in use by another program.{modem}"
        )

    async def link_present(self) -> bool:  # noqa: D102 - inherited docstring
        if self._mc is None:
            return True  # not connected yet, or torn down already: nothing to declare lost
        if self._transport in ("ble", "tcp"):
            # The meshcore client sets ``is_connected`` to False at the moment the transport
            # is lost (bleak's disconnect callback for BLE, a broken socket for TCP). Thus
            # this is the network and Bluetooth equivalent of the check of the serial port
            # list: a fast liveness read that does not transmit.
            try:
                return bool(self._mc.is_connected)
            except Exception:  # noqa: BLE001 - a status hiccup must not fake a disconnect
                return True
        if not self._port:
            return True  # no port stored (it should not occur after a connect): cannot tell
        # Run this off the event loop, as all the other callers of the port walk do. On the
        # PicoCalc, it is approximately 30 ms of sysfs reads when nothing else runs. But each
        # of its hundreds of small reads releases the GIL, and must wait its turn to get it
        # again. While a worker thread draws a map frame, that turn can come up to 5 ms
        # later. On the loop, this check every two seconds froze the screen for up to 1.2 s
        # each time.
        return await asyncio.to_thread(serial_port_present, self._port)

    async def disconnect(self) -> None:
        """Close the connection and release the resources. Idempotent, with a time limit.

        The graceful ``meshcore`` teardown gets :data:`_DISCONNECT_TIMEOUT_S` to complete.
        That limit is important. The dispatcher stop of the library awaits ``queue.join()``,
        but after the stop, its processor task exits after it handles a maximum of one
        event. Thus, with two or more events in the queue at that time (a usual burst of
        adverts and RX log entries on a live mesh), the join deadlocks. A quit would then
        hang until the exit watchdog kills the process.

        When the graceful path does not return in time, MeshTerm cancels it and forces the
        teardown instead. It cancels the dispatcher task synchronously, and closes the raw
        transport directly (also with a time limit). Thus the port or the link is still
        released.

        Over Bluetooth, MeshTerm then also closes the ``bleak`` client directly, because
        none of the steps above reaches it when the peripheral is what went away. Refer to
        :meth:`_release_ble_client`.
        """
        if self._mc is None:
            await self._release_ble_client()  # a link that was lost before we tore it down
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
            # Forced teardown: cancel the dispatcher task (which can be blocked), and do not
            # await the deadlocked join. Then close the transport below, so that the serial
            # port or the BLE link is in fact released. Best-effort: no step here must block
            # the exit.
            await MeshCoreDevice._force_close_transport(mc)
        except Exception as exc:  # noqa: BLE001 - a teardown must not raise. Use the forced path.
            _log.debug("graceful disconnect failed (%s); forcing transport teardown", exc)
            await MeshCoreDevice._force_close_transport(mc)
        await self._release_ble_client()

    async def _release_ble_client(self) -> None:
        """Close the ``bleak`` client itself, whatever the library thinks about its state.

        This is the only teardown step that MeshTerm cannot delegate. It is also the reason
        why a Bluetooth session could not be built again after the companion was switched
        off. When the *peripheral* is what went away, no layer above acts, each for its own
        reason that is sensible locally:

        * ``BLEConnection.handle_disconnect`` (bleak's own callback for a lost link)
          restores the fields of the connection to the values that the caller first gave.
          This sets ``self.client`` back to ``None``. Nothing holds the live client object
          after that.
        * ``BLEConnection.disconnect`` then checks ``self.client and
          self.client.is_connected``, so it has nothing to close and does nothing.
        * ``ConnectionManager.disconnect`` checks ``self._is_connected``, which its own
          handler for a lost link already cleared. Thus it does nothing too.
        * :meth:`_force_close_transport` goes past both, but only as far as that same
          ``BLEConnection``, whose ``disconnect`` is the no-op above.

        Thus, on a lost link, nothing ever calls ``BleakClient.disconnect()``. On Windows,
        the WinRT ``BluetoothLEDevice`` and its GATT session stay open for the life of the
        process. The next connect to that address then gets a broken service table. The
        link opens, and the subscribe to the UART characteristic fails with
        *"Characteristic 6E400003-… was not found!"*. The device advertises and is healthy,
        but MeshTerm cannot reach it while the app runs. A new process connects to it with
        no problem.

        This is why we keep our own reference. We take it during the connect (refer to
        :meth:`_connect_owned_ble`), while the library still has one to give, and we close
        it here **always**. The close never checks ``is_connected``, because the important
        case is exactly the case where it is already ``False``. The close has a time limit
        and is silent: the link is already gone, and a teardown must not raise or hang. It
        is idempotent, and it does nothing on serial and TCP, which have no client to hold.
        """
        client, self._ble_client = self._ble_client, None
        if client is None:
            return
        disconnect = getattr(client, "disconnect", None)
        if disconnect is None:  # pragma: no cover - every bleak client has one
            return
        try:
            await asyncio.wait_for(disconnect(), timeout=_FORCE_DISCONNECT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - the link is already gone. Best-effort.
            _log.debug("releasing the bleak client failed: %s", exc)

    def _require(self):  # type: ignore[no-untyped-def]
        """Return the live client, or raise an error if it is not connected."""
        if self._mc is None:
            raise RuntimeError("Device is not connected; call connect() first.")
        return self._mc

    async def get_self_info(self) -> dict:  # noqa: D102 - inherited docstring
        mc = self._require()
        result = await mc.commands.send_appstart()
        info = dict(getattr(result, "payload", {}) or {})
        # The firmware sends the TX power as a signed byte (it accepts down to -9 dBm), and
        # the library reads it as unsigned. Thus a negative power would arrive as 247 or more.
        power = info.get("tx_power")
        if isinstance(power, int) and power > 127:
            info["tx_power"] = power - 256
        return info

    async def get_device_info(self) -> dict:  # noqa: D102 - inherited docstring
        mc = self._require()
        try:
            result = await mc.commands.send_device_query()
        except Exception:  # noqa: BLE001 - older firmware has no query: unknown, not fatal
            return {}
        return dict(getattr(result, "payload", {}) or {})

    #: The maximum silence of the contacts stream before MeshTerm considers a read as
    #: failed (seconds). The library's own ``get_contacts`` arms one future for the *full*
    #: dump, and never arms it again. Thus a table that takes more than its five seconds to
    #: stream fails with "no event received", also when the radio is healthy. A node with
    #: four hundred contacts does this over Bluetooth. What in fact means "the companion
    #: stopped answering" is a gap *between* records, so this value times that gap. The
    #: dump can take as long as necessary, while it continues to arrive.
    _CONTACTS_IDLE_S = 6.0

    async def _contacts_payload(
        self, mc, *, retries: int = 3, delay: float = 0.5, idle: float | None = None
    ) -> dict:  # noqa: ANN001
        """Read the raw contacts map, and try again after a read that stops or is refused.

        The companion sometimes refuses or loses a contacts request. This is a timing
        problem that MeshTerm can recover from. Thus MeshTerm tries the read a few times,
        with a short backoff, before it raises a clean error that the user can act on. One
        try is :meth:`_stream_contacts`.

        Args:
            mc: The connected ``MeshCore`` client.
            retries: The number of more tries after the first.
            delay: The seconds to wait between tries.
            idle: The seconds of silence that end a try (default :data:`_CONTACTS_IDLE_S`).

        Returns:
            The contacts payload mapping (it can be empty).

        Raises:
            DeviceCommandError: If each try fails to get the contacts.
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
        """Run one contacts read, and end it on *silence*, not on a deadline.

        The contacts table arrives as one ``NEXT_CONTACT`` frame for each record, and a last
        ``CONTACTS`` frame that holds the full map. Thus a dump is not one answer that is
        late or on time. It is a stream. The only difference between a slow big table and
        a radio that has stopped is the time since the last record. Thus each record starts
        the clock again, and only ``idle`` seconds of silence end the try. This is the full
        repair for a node with four hundred contacts, whose dump takes more time than any
        fixed deadline that the library would give it.

        Args:
            mc: The connected ``MeshCore`` client.
            idle: The seconds of silence that end the try.

        Returns:
            ``(payload, "")`` on success, or ``(None, reason)``, which tells how it ended.
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
            # An ERROR frame has no request id, so it is ours only while nothing else can
            # have caused it. Before the first record, it is the refusal of this request (a
            # companion that is too busy to serve a dump answers ERR_CODE_BAD_STATE). After
            # the first record, it is for something else on the link (a battery poll, a
            # courier send). When MeshTerm treated it as ours, a read that worked stopped.
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

    #: The number of bytes of a name in the contact record of the firmware (a 32-byte field).
    _CONTACT_NAME_BYTES = 32

    async def add_contact(self, node: Contact) -> None:  # noqa: D102 - inherited docstring
        mc = self._require()
        pub = self._node_pubkey(node)  # raises DeviceCommandError if it has no key
        if len(pub) != 64:
            raise DeviceCommandError(
                f"{node.name} is known only by a key prefix, so it can't be added to the "
                "device — receive an advert from it first."
            )
        # The library writes contacts through one add-or-update command, from a record with
        # exactly the shape that a contacts read gives. A flood route (``out_path_len`` -1)
        # is what a contact that the device just heard has, and the device learns a path
        # again from received traffic, as usual.
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
            # A contact that the firmware does not have is the only rejection that is not
            # a failure. The list from which a screen removes is the union of this table and
            # the contacts that MeshTerm remembers for the device. Thus the entry that the
            # user deletes may have existed only on our side. This is the same class that
            # the send path raises, for the same reason: the caller can complete the job.
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
        # The firmware reads a signed byte. The library packs an unsigned int, which refuses
        # a negative value. The two's complement has the same low byte, so -9 dBm arrives
        # correctly.
        await mc.commands.set_tx_power(int(value) & 0xFFFFFFFF)

    @staticmethod
    def _node_pubkey(node: Contact) -> str:
        """Return the full public key of a contact, for remote addressing.

        Args:
            node: The contact to address.

        Returns:
            The lowercase hex public key (with no ``0x``).

        Raises:
            DeviceCommandError: If the contact has no public key, so MeshTerm cannot
                address it for login or admin commands.
        """
        pub = (node.public_key or "").lower().removeprefix("0x")
        if not pub:
            raise DeviceCommandError(
                f"contact {node.name!r} has no public key on this device, so it can't be "
                "logged in to for admin commands. Receive an advert from it first."
            )
        return pub

    async def admin_login(self, node: Contact, password: str) -> LoginResult:  # noqa: D102
        return (await self._login(node, password, what="admin login")).result

    async def room_login(self, room: Contact, password: str) -> RoomLogin:  # noqa: D102
        exchange = await self._login(room, password, what="room login")
        access = None
        if exchange.result is LoginResult.ACCEPTED:
            access = RoomAccess.from_login(exchange.payload or {})
        return RoomLogin(
            exchange.result, access, flood=exchange.flood, radio_error=exchange.radio_error
        )

    async def reset_route(self, node: Contact) -> None:  # noqa: D102 - inherited docstring
        mc = self._require()
        pub = self._node_pubkey(node)
        result = await mc.commands.reset_path(pub)
        if result is None or getattr(result, "is_error", lambda: False)():
            if error_code(result) == _ERR_NOT_FOUND:
                raise ContactNotOnDeviceError(node)
            raise DeviceCommandError(
                f"couldn't reset the route to {node.name}: {reject_reason(result)}"
            )

    async def _login(self, node: Contact, password: str, *, what: str) -> _LoginExchange:
        """Run one login exchange with a remote node, and report how it ended.

        A remote node has one login, whatever role the password gets. :meth:`admin_login`
        and :meth:`room_login` each read its answer in their own way.

        Args:
            node: The node to log in to.
            password: The password to give.
            what: The name of the exchange in the debug log.

        Returns:
            The result, the payload of the ``LOGIN_SUCCESS`` reply that accepted the login,
            and how the radio sent the request (refer to :class:`_LoginExchange`).
        """
        from meshcore import EventType

        mc = self._require()
        pub = self._node_pubkey(node)
        loop = asyncio.get_running_loop()
        # Listen for the answer of the node *before* the transmission, and continue to listen
        # during the full exchange. :meth:`run_trace` follows this rule, for the same two
        # reasons, and ``send_login_sync`` breaks both of them.
        #
        # ``send_login_sync`` waits for LOGIN_SUCCESS and *only* LOGIN_SUCCESS, and it does
        # not start to wait until its own send has returned. Thus:
        #
        # * It never waits for a refusal. (The firmware does send a refusal, as a
        #   LOGIN_FAILED frame that the library parses and dispatches like any other
        #   event.) The refusal times out exactly like a node that MeshTerm cannot reach.
        #   Both types of failure came back as the same ``None``.
        # * A worse case, for a node that MeshTerm can reach with no problem: the send
        #   itself blocks until a MSG_SENT arrives. The library correlates that
        #   acknowledgement only by its event type. Thus a scheduled advert, a telemetry
        #   poll, or the courier can take our MSG_SENT, and leave the send on its own
        #   15-second default. Each answer that arrives during that block goes to no
        #   listener and is lost. Thus MeshTerm stores a login that the repeater *accepted*
        #   as no reply. The opposite case is as bad: when the send catches the MSG_SENT of
        #   another command, it takes the ``suggested_timeout`` of that command. For a
        #   neighbour, this is one or two seconds, which is much less than the round trip
        #   of a multi-hop repeater. This is the reported bug: a healthy node, the right
        #   password, and a no-reply dialog.
        #
        # When we own the wait, both problems go away. ``send_login_sync`` still transmits
        # (it is the supported path of the library, and its own listener causes no problem,
        # because the dispatcher delivers to each matching subscription). But our listener
        # is armed first, and it lives longer. When the listener of the library stops
        # early, the node still gets a full budget of its own. The budget is sized to the
        # route, as for a trace to the same node, and it is counted from the send, not from
        # the queuing. Refer to the wait below for why that difference is the repair.
        #
        # The return value of ``send_login_sync`` comes from the library's own wait for the
        # *answer* (it gives the node ``suggested_timeout / 800`` seconds). It never tells
        # if the request went out. Thus MeshTerm hears the confirmation of the radio here,
        # on its own listener. On hardware, the confirmation arrives within a tenth of a
        # second, while the library's wait continues for six seconds. In the past, this
        # log called each such login "unacknowledged" (checked with a room on 2026-10-05,
        # whose acceptance then arrived ten seconds after the send). The confirmation also
        # tells how the radio sent the request: as a flood, or along the route that it had
        # learned. This tells an old route from a silent node.
        answer: asyncio.Future = loop.create_future()
        # The confirmation of *this* request from the radio. The ``expected_ack`` of a login
        # is the first four bytes of the addressed key (MeshCore's ``pending_login``). This
        # value tells it from the MSG_SENT of any other command that is in progress.
        confirm: asyncio.Future = loop.create_future()
        ours = bytes.fromhex(pub[:8])
        errors: list[object] = []
        # Frames that arrived during the exchange but are not our result. MeshTerm keeps
        # them only to log them with the result. A "no reply" with one of these frames next
        # to it is a different fault from a node that stayed silent, and nothing else would
        # show it.
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
                # An answer that names another node (two admin flows can overlap). We must
                # not act on it, but it is useful to log that we heard it. A login that
                # reports silence, with one of these in the log, is a key prefix that did
                # not match, not a silent node.
                note("login frame for another node:", event)

        def on_local_error(event) -> None:  # noqa: ANN001 - meshcore Event
            # The only failure that never reaches the mesh: the companion refuses to send.
            # ``send_login_sync`` removes it. It changes its own ERROR into a bare ``None``,
            # which looks the same as an acknowledgement that was only slow. Thus the only
            # way to get the reason is to listen for the frame. The frame is uncorrelated (an
            # ERROR has no request id, and can be for another command that is in progress).
            # Thus it becomes the reason only when the radio never confirmed that it sent
            # this request.
            note("companion error:", event)
            errors.append(event)

        def on_sent(event) -> None:  # noqa: ANN001 - meshcore Event
            payload = getattr(event, "payload", None) or {}
            if not confirm.done() and payload.get("expected_ack") == ours:
                confirm.set_result((loop.time(), payload))

        subscriptions = [
            mc.subscribe(EventType.LOGIN_SUCCESS, on_answer),
            mc.subscribe(EventType.LOGIN_FAILED, on_answer),
            mc.subscribe(EventType.ERROR, on_local_error),
            mc.subscribe(EventType.MSG_SENT, on_sent),
        ]
        # A login is a round trip along the route of the contact and back. A trace budget
        # already describes this shape. The stored route is one-way, so the full exchange
        # has twice its hops. A known route can only give *more* time, never less. An
        # admin exchange is heavier than the one small packet of a trace. Thus a contact for
        # which we have a short route must not get a shorter window than a contact without
        # a route. ``trace_timeout(0)``, the flood budget, is exactly that window.
        hops = 2 * len(node.route_hops or ())
        budget = max(trace_timeout(hops), trace_timeout(0))
        started = loop.time()
        try:
            async with self.transmitting():
                library = await mc.commands.send_login_sync(pub, password)
            # The budget starts *after* ``send_login_sync`` returns, never at the moment when
            # we started to queue. All the time before that is for the companion and the
            # library, and it can be most of a minute. The library waits its turn on its
            # mesh-request lock (each telemetry poll and courier retry holds this lock for a
            # full round trip). Then it waits for the confirmation of the radio. Then it
            # waits for the answer of the node, ``suggested_timeout / 800`` seconds (for a
            # long time, this log thought that this part was the send). Thus a node gets that
            # library wait *and* the budget: approximately six seconds, and ten for a flood.
            # A room five hops away needed this (its answers arrived ten seconds after the
            # confirmation, on hardware). An earlier repair counted the time from the
            # queuing. That repair made the wait longer, but the login still failed at the
            # same ten seconds, because something else had already used the window.
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
        if event is None and getattr(library, "type", None) is EventType.LOGIN_SUCCESS:
            # This usually cannot occur: our subscription was registered first, so our
            # listener saw all that the library's own wait saw. But to use the library
            # result costs nothing. It can only change a false no-reply into the acceptance
            # that it was in fact, and this method prefers to fail in that direction.
            event = library
        etype = getattr(event, "type", None)
        confirmed_at, confirmation = confirm.result() if confirm.done() else (None, {})
        flood = None if confirmed_at is None else confirmation.get("type") == 1
        # Log where the time went, because a no-reply has three different causes, and the
        # time tells them apart:
        # * A confirmation that took seconds means that the request waited behind another
        #   command.
        # * No confirmation at all means that the companion never transmitted the request
        #   (and the log shows the error that it raised, if any).
        # * A full wait after a fast confirmation means that the node in fact stayed silent.
        answered = loop.time()
        _log.debug(
            "%s to %s: %s, budget=%.1fs -> %s %.0fms after %s%s",
            what,
            node.name,
            "never confirmed sent"
            if confirmed_at is None
            else f"sent by {'flood' if flood else 'its route'}, confirmed in "
            f"{(confirmed_at - started) * 1000.0:.0f}ms",
            budget,
            etype,
            (answered - (confirmed_at or started)) * 1000.0,
            "the send" if confirmed_at is not None else "queuing it",
            f" [also heard: {'; '.join(stray)}]" if stray else "",
        )
        radio_error = None
        if confirmed_at is None and errors:
            radio_error = (
                "this node isn't in the radio's contacts"
                if error_code(errors[-1]) == _ERR_NOT_FOUND
                else reject_reason(errors[-1])
            )
        if etype is EventType.LOGIN_SUCCESS:
            return _LoginExchange(
                LoginResult.ACCEPTED, dict(getattr(event, "payload", None) or {}), flood
            )
        if etype is EventType.LOGIN_FAILED:
            return _LoginExchange(LoginResult.REFUSED, None, flood)
        # Nothing came back. This includes the local-ERROR case, where the companion did not
        # even send the request. In both cases, we never heard the node. Thus the password
        # is not proved correct and not proved wrong, and the caller must keep it.
        return _LoginExchange(LoginResult.NO_REPLY, None, flood, radio_error)

    @staticmethod
    def _refers_to(event: object, pubkey: str) -> bool:
        """Is this login frame about the node that we addressed?

        The firmware puts the 6-byte key prefix of the *answering node* on a login reply,
        when the frame is long enough for it. Older or shorter frames arrive without it.
        Thus this method matches when there is something to match, and accepts the frame
        in other cases. If it demanded a key prefix, it would silently change each answer
        from short firmware into a no-reply. That is the failure that this full path must
        prevent.
        """
        payload = getattr(event, "payload", None) or {}
        prefix = str(payload.get("pubkey_prefix") or "").lower().removeprefix("0x")
        return not prefix or pubkey.lower().startswith(prefix)

    async def _send_admin_cmd(self, node: Contact, cmd: str, *, timeout: float = 8.0):
        """Send a CLI command to a remote node where we are logged in, and wait for its reply.

        The companion acknowledges the send immediately (``MSG_SENT``). The text reply of
        the node arrives later as a ``CONTACT_MSG_RECV`` event. We return that reply text
        (or ``None`` if no reply arrived before ``timeout``).

        The reply is the first message *from this node* that is not a room post. In the
        past, any direct message counted. Thus a message from a companion that arrived
        during the wait was taken as the answer of the repeater. Also, a room server pushes
        the posts of its members down the same channel, one after another as each is
        acknowledged. Thus, when you administered a room where you are a member, MeshTerm
        read the post of another user as the result of ``get``. The listener is armed
        before the command goes out, as for the login. Thus MeshTerm also does not miss a
        reply that is faster than the return of the send.

        Args:
            node: The remote contact (it must already be logged in).
            cmd: The repeater CLI command, for example ``"set tx 20"``.
            timeout: The seconds to wait for the reply of the node.

        Returns:
            The reply text, or ``None`` if the node did not answer in time.

        Raises:
            DeviceCommandError: If the companion rejected the send command itself.
        """
        from meshcore import EventType

        mc = self._require()
        pub = self._node_pubkey(node)
        answer: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_message(event) -> None:  # noqa: ANN001 - meshcore Event
            payload = getattr(event, "payload", None) or {}
            if answer.done() or payload.get("txt_type") == _TXT_TYPE_SIGNED_PLAIN:
                return
            answer.set_result(payload)

        # The firmware puts the six-byte key prefix of the sender on a received direct
        # message. The library stores it as the ``pubkey_prefix`` attribute of the event.
        subscription = mc.subscribe(
            EventType.CONTACT_MSG_RECV, on_message, {"pubkey_prefix": pub[:12]}
        )
        try:
            async with self.transmitting():
                sent = await mc.commands.send_cmd(pub, cmd)
            if sent is not None and getattr(sent, "is_error", lambda: False)():
                raise DeviceCommandError(
                    f"couldn't send admin command {cmd!r} to {node.name}: {reject_reason(sent)}"
                )
            try:
                payload = await asyncio.wait_for(asyncio.shield(answer), timeout)
            except asyncio.TimeoutError:
                return None
        finally:
            subscription.unsubscribe()
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
        # The reply ("ok", or the value again) is a best-effort confirmation. If it is
        # absent, that is not fatal, because some firmware answers briefly, or loses the
        # ack under duty-cycle limits.
        await self._send_admin_cmd(node, f"set tx {value}")

    async def fetch_neighbours(self, node: Contact) -> list[NeighbourInfo]:  # noqa: D102
        mc = self._require()
        pub = self._node_pubkey(node)
        # The library reads the table in pages (one binary request for each approximately
        # 25 entries), and joins them. ``min_timeout`` prevents a cut of slow multi-hop
        # replies at the optimistic suggested timeout of the companion.
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
        # ``min_timeout`` for the same reason as in the neighbour request: the suggested
        # timeout of the companion is optimistic for all nodes except a neighbour.
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
        from meshcore import EventType  # local import: the mock path runs without meshcore

        mc = self._require()
        tag = random.randint(0, 0xFFFFFFFF)
        loop = asyncio.get_running_loop()
        started = loop.time()

        # A trace packet has no destination field. It walks an explicit path of repeater
        # hops. Send the path as raw bytes, so that the radio can transmit any uniform hash
        # width. ``flags`` has the path-hash mode (size - 1).
        path_bytes: bytes | None = None
        flags = 0
        if path:
            hops = [h.strip() for h in path.split(",") if h.strip()]
            path_bytes = bytes.fromhex("".join(hops))
            flags = path_hash_flags(len(bytes.fromhex(hops[0]))) or 0
        else:
            # No forced path: build a path that ends at the own hash of the target (with
            # any learned ``out_path`` repeaters before it), because a trace replies only
            # when its destination is the last hop. It is ``None`` only when the contact
            # is unknown, and then the trace runs without a path.
            resolved = await self._trace_path_to_contact(mc, target)
            if resolved is not None:
                path_bytes, flags = resolved

        # Size the wait for the reply to the route, unless the caller set it. ``path_bytes``
        # is the full walk (out, and the mirrored return leg). Thus its number of entries
        # (each ``1 << flags`` bytes wide) is the number of relay transmissions of the
        # packet before the reply reaches us. A flood without a path leaves the count
        # unknown (0).
        if timeout is None:
            hops_walked = len(path_bytes) // (1 << flags) if path_bytes else 0
            timeout = trace_timeout(hops_walked)

        # Listen for our tag *before* the transmission, and continue to listen during the
        # full trace. ``send_trace`` does not return until the ``MSG_SENT`` of the
        # companion arrives, and the library correlates that acknowledgement only by its
        # event type. Thus any other command that is in progress (a scheduled advert, a
        # telemetry poll, the courier) can take our acknowledgement, and leave the send
        # blocked on its own 15-second default. If we subscribed after the send, each
        # reply that arrived during that block would go to no listener and be lost. The
        # trace would then be stored as "no reply", although the mesh answered it. That is
        # why a burst of background traffic made *each* trace fail while the burst
        # continued, not only the trace that it collided with. The handler stores the
        # arrival time of the reply itself, so a blocked send does not make a round trip
        # longer.
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
                # Uncorrelated acknowledgements make this ambiguous (the error can be for
                # a completely different command). Thus it is evidence, not a result: the
                # trace is still on the air, and its reply can still arrive.
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
        # The firmware addresses each hop by a hash of ``1 << flags`` bytes. Store this
        # width, so the summary can show node hashes at the width that the command used.
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
        """Find the trace ``(path_bytes, flags)`` for a target contact.

        A trace reply comes back only when the *own hash of the destination* is the last
        hop in the path. The firmware silently ignores an empty path, or a path without the
        destination. (We checked this on hardware: a direct neighbour answers a single-hop
        trace to its own hash, but not a trace without a path.) Thus we always end the
        outbound leg at the hash of the contact, with any learned repeater hops
        (``out_path``) before it. The trace protocol has no separate field for the return
        path. Thus we also mirror those same repeaters back after it (refer to
        :func:`~meshterm.services.topology.render_forced_spec`, which does the same for a
        composed or adopted path). Without an explicit return leg, the repeaters have
        nothing through which to relay the reply, so it never comes back.

        * direct neighbour, or no learned route → only ``[destination]`` (there are no
          repeaters to mirror, so the outbound leg is the full path).
        * learned multi-hop route → ``[repeater…, destination, repeater… (reversed)]``.

        Each hash is encoded again at the own width of the trace (``1 << flags``, only
        1/2/4/8 bytes). This changes the routing width of a region (for example 3) to the
        widest value that the trace can represent (2). The firmware matches by hash
        prefix, so a narrower prefix still addresses the same node.

        Args:
            mc: The connected ``MeshCore`` client.
            target: The contact name (not case-sensitive), or a key prefix.

        Returns:
            ``(path_bytes, flags)`` to walk, or ``None`` only when the contact is
            unknown or has no public key to address.
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
                return None  # no key to address the destination hop of the trace

            # The routing hash width (bytes): the stored mode of the contact, or the mode
            # of our region when the contact has no learned route (mode == -1).
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
                # Learned multi-hop route: walk each repeater, cut to the trace width.
                route = bytes.fromhex(out_path)[: out_path_len * size]
                repeaters = [route[i * size : i * size + trace_size] for i in range(out_path_len)]
            dest = bytes.fromhex(pub)[:trace_size]
            # The outbound leg always ends at the own hash of the destination, so the
            # destination recognizes the trace and replies. The return leg mirrors the same
            # repeaters back to us, because nothing sends the packet back automatically.
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

        # Subscribe to the event types that this firmware and library build has. The field
        # names of the event payload that the mappers read are best-effort. As for the
        # trace mapping, check them with the event schema of your firmware.
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
        # The RX packet log of the companion, when its firmware has packet logging on. Each
        # overheard packet arrives with the relay path that it went through. This is the
        # passive topology evidence from which the trace path composer suggests hops.
        # Firmware without RX logging never pushes these events. The subscription costs
        # nothing in both cases.
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
        # Trace replies are not observations (the ``run_trace`` that sent the trace takes
        # them by tag). But a log line for each reply that arrives lets us diagnose a "no
        # reply". A reply logged here with no matching ``trace <tag>`` line means that the
        # walk came back and we did not listen. That is a different fault from silence on
        # the air.
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

        # Do the pull of received messages ourselves (refer to ``_message_pump``). MeshCore
        # never pushes message bodies, so without this pull, a send works but nothing is
        # received.
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
        """Pull the received messages from the companion continuously (the RX pull model).

        MeshCore does not push message bodies unsolicited. The device sends a
        ``MESSAGES_WAITING`` notification, and the client must call ``get_msg()`` to get
        each queued message. The reader of the library then dispatches it as
        ``CONTACT_MSG_RECV`` or ``CHANNEL_MSG_RECV`` to the handler registered above. (The
        temporary listener of a command does not take the event: each subscriber still
        gets it.)

        We do that pull in three ways, so that it is robust across firmware builds:

        * An immediate drain (it delivers all that is already in the queue).
        * A drain on each ``MESSAGES_WAITING`` push (low latency).
        * A slow timer (a safety net for builds whose pushes are not reliable: the failure
          that this repairs).

        A lock serializes the drains, so the triggers that overlap never send concurrent
        ``get_msg`` commands.

        Args:
            mc: The connected ``MeshCore`` client.
            subs: The subscription list to which this method adds the ``MESSAGES_WAITING``
                subscription (so it is torn down with the others).
            subscribe: The ``subscribe`` callable of the client.

        Returns:
            A callable with no arguments that stops the pump (its poll task and drains).
        """
        from meshcore import EventType

        stop = asyncio.Event()
        draining = asyncio.Lock()

        async def drain() -> None:
            # Pull until the device reports that the queue is empty. The dispatch of the
            # reader delivers each message to our handler. Thus the only thing to do with
            # the returned event is to check whether to continue.
            async with draining:
                while not stop.is_set():
                    try:
                        event = await mc.commands.get_msg(timeout=_MESSAGE_GET_TIMEOUT_S)
                    except Exception as exc:  # noqa: BLE001 - temporary. The poll tries again.
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
                    await drain()  # the interval ended: look for all that the push missed

        waiting = getattr(EventType, "MESSAGES_WAITING", None)
        if waiting is not None:
            subs.append(subscribe(waiting, schedule_drain))
        schedule_drain()  # a first, immediate drain of all that is already in the queue
        poll_task = asyncio.ensure_future(poll_loop())

        def stop_pump() -> None:
            stop.set()  # ends the poll loop and any drain in progress at the next check
            poll_task.cancel()

        return stop_pump

    async def send_direct_message(  # noqa: D102 - inherited docstring
        self, contact: Contact, text: str
    ) -> Delivery:
        transmit_gate.mark()
        from meshcore import EventType

        mc = self._require()
        pub = self._node_pubkey(contact)
        loop = asyncio.get_running_loop()
        # Listen before the send, and to each ack. MeshTerm knows the code to match only
        # after the send returns. In the past, an ack faster than that return (from a
        # neighbour) arrived before anything listened for it. MeshTerm keeps each ack by
        # its code until it knows the code.
        heard: dict[str, object] = {}
        wanted: asyncio.Future = loop.create_future()
        code: list[str] = []
        flood = False
        budget = 0.0

        def on_ack(event) -> None:  # noqa: ANN001 - meshcore Event
            got = str((getattr(event, "payload", None) or {}).get("code") or "").lower()
            heard[got] = event
            if code and got == code[0] and not wanted.done():
                wanted.set_result(event)

        subscription = mc.subscribe(EventType.ACK, on_ack)
        started = loop.time()
        try:
            # Only the hand-over holds the transmit lock. The ack wait below is not a send.
            async with self.transmitting():
                result = await mc.commands.send_msg(pub, text)
            if result is None or getattr(result, "is_error", lambda: False)():
                # A recipient for which the firmware has no entry is the only rejection with
                # an obvious repair. Thus it gets its own class, so the chat screen can offer
                # that repair. Refer to :class:`ContactNotOnDeviceError` for how a contact in
                # the list can be missing here.
                if error_code(result) == _ERR_NOT_FOUND:
                    raise ContactNotOnDeviceError(contact)
                raise DeviceCommandError(
                    f"couldn't send to {contact.name}: {reject_reason(result)}"
                )
            # The companion accepts the send immediately, with the ``expected_ack`` code and
            # how it sent the message. The ack of the recipient arrives later, with the code.
            payload = getattr(result, "payload", {}) or {}
            expected = payload.get("expected_ack")
            expected_hex = expected.hex() if isinstance(expected, (bytes, bytearray)) else expected
            if not expected_hex:
                return Delivery(None)
            code.append(str(expected_hex).lower())
            if code[0] in heard:
                wanted.set_result(heard[code[0]])
            flood = payload.get("type") == 1
            # Wait as long as for a login, whose answer travels exactly as an ack does: the
            # own estimate of the radio (``suggested_timeout``, which the library makes a
            # quarter longer), then a round trip sized to the route. The estimate alone (this
            # code waited only that long until 2026-10-05) is ``500 ms + 16 x airtime`` for
            # a flood, approximately five seconds. But on hardware, the answer to a flood
            # from five hops away took ten seconds. Thus most acks from farther than a
            # neighbour arrived after the wait had stopped, and four messages of five that
            # had arrived showed as failed.
            hops = 0 if flood else 2 * len(contact.route_hops or ())
            suggested = float(payload.get("suggested_timeout") or 0) / 1000.0
            budget = suggested * 1.25 + max(trace_timeout(hops), trace_timeout(0))
            if not wanted.done():
                try:
                    await asyncio.wait_for(asyncio.shield(wanted), budget)
                except asyncio.TimeoutError:
                    pass
        finally:
            subscription.unsubscribe()
        event = wanted.result() if wanted.done() else None
        ack = ack_from_event(event) if event is not None else None
        _log.debug(
            "direct message to %s: sent by %s, code %s, budget=%.1fs -> %s",
            contact.name,
            "flood" if flood else "its route",
            code[0],
            budget,
            f"acked in {(loop.time() - started) * 1000.0:.0f}ms "
            f"(the radio's round trip: {(ack.raw or {}).get('trip_time')}ms)"
            if ack is not None
            else "no ack yet",
        )
        return Delivery(code[0], ack, flood)

    async def send_channel_message(  # noqa: D102 - inherited docstring
        self, index: int, text: str
    ) -> None:
        transmit_gate.mark()
        async with self.transmitting():
            self._ok(await self._require().commands.send_chan_msg(index, text))

    @staticmethod
    def _ok(event):  # type: ignore[no-untyped-def]
        """Return ``event`` if it succeeded, or else raise its error payload.

        Args:
            event: The :class:`meshcore.events.Event` that a command returned.

        Returns:
            The same event, so that calls can be chained.

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
        # airtime_factor * 1000). Undo that scaling, so callers see the real values.
        return {
            "rx_delay": int(payload.get("rx_delay", 0)) / 1000.0,
            "airtime_factor": int(payload.get("airtime_factor", 0)) / 1000.0,
        }

    #: The hop limit that arrived in the same frame as the last bitmask, until a caller takes it.
    _autoadd_hops: object = _UNREAD

    async def get_autoadd_config(self) -> int | None:  # noqa: D102 - inherited docstring
        mc = self._require()
        # One reply has the bitmask *and* the hop limit, but the parser of the library keeps
        # only the first. Thus MeshTerm taps the reader for the time of this one read. Each
        # transport gives frames to ``reader.handle_rx`` through an attribute lookup, so an
        # instance attribute gets them first. MeshTerm keeps the second byte of the raw
        # frame.
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
        # build_snapshot asks for the bitmask, and then for this value. The frame that
        # answered the first question already had the second, so MeshTerm takes it from
        # there, and does not ask again.
        if self._autoadd_hops is _UNREAD:
            await self.get_autoadd_config()
        hops, self._autoadd_hops = self._autoadd_hops, _UNREAD
        return hops  # type: ignore[return-value]

    async def get_allowed_repeat_freqs(self) -> list[tuple[float, float]]:  # noqa: D102
        event = self._ok(await self._require().commands.get_allowed_repeat_freq())
        payload = getattr(event, "payload", {}) or {}
        # Ranges go in kHz, as all the other frequencies on this protocol do.
        return [
            (int(r["min"]) / 1000.0, int(r["max"]) / 1000.0) for r in payload.get("freqs") or []
        ]

    async def get_default_flood_scope(self) -> str | None:  # noqa: D102
        event = self._ok(await self._require().commands.get_default_flood_scope())
        payload = getattr(event, "payload", {}) or {}
        name = payload.get("scope_name")
        # Return the name bare, whatever client wrote it. MeshTerm (before it built its own
        # frames) and the library (still now) stored a ``#`` that the firmware and the apps
        # do not use.
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
        # Only for BLE, and only when a device has the standard Battery Level Status
        # characteristic. No MeshCore firmware has it now. Thus this returns None on all
        # current devices, and the battery poller uses its voltage-trend inference instead.
        # It reaches the raw bleak client in the same way as the forced-teardown path
        # (through ``connection_manager.connection``), and all of it is in one try block.
        # Any failure (a changed attribute path, no service, a refused read, a short value)
        # gives a silent None, and never interrupts the poll.
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
        # Each frame is best-effort on its own. Firmware that is older than one statistics
        # type answers it with an error, and that error must not cost us the frames that
        # the firmware supports.
        for read in (
            mc.commands.get_stats_core,
            mc.commands.get_stats_radio,
            mc.commands.get_stats_packets,
        ):
            try:
                event = self._ok(await read())
            except Exception:  # noqa: BLE001 - an optional read. Its absence is acceptable.
                continue
            stats.update(getattr(event, "payload", {}) or {})
        return stats

    async def get_path_hash_mode(self) -> int:  # noqa: D102 - inherited docstring
        return int(await self._require().commands.get_path_hash_mode())

    async def get_custom_vars(self) -> dict[str, str]:  # noqa: D102 - inherited docstring
        event = self._ok(await self._require().commands.get_custom_vars())
        return dict(getattr(event, "payload", {}) or {})

    async def get_channel(self, index: int) -> dict | None:  # noqa: D102
        # The read is serialized (refer to self._channel_read_lock), so a concurrent read
        # cannot take the uncorrelated CHANNEL_INFO response. We still check that the
        # response is for the slot that we asked for. If not, a stray CHANNEL_INFO (from
        # another source, or a slow response that arrives after a timeout) would identify
        # the wrong slot, and the messages of a channel would go to the wrong place. On a
        # mismatch, we raise an error, and do not return the data of another slot.
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
        """Read one slot, and tell the "no such slot" of the firmware from any other error.

        The slot probe ends on a *rejection*, and keeps the list that it has as the full
        layout of the device. Thus it is important what counts as a rejection. The firmware
        answers an index after its last slot with ``ERR_CODE_NOT_FOUND`` and nothing else.
        But the library does not serialize commands, and each command that waits for "OK
        or ERROR" takes the first ERROR that arrives. Thus a read that races another
        command can get the refusal of *that* command. At the connect, the channel prewarm
        runs at the same time as the clock sync. A clock that the firmware refused as
        malformed ended the probe at the slot that it had reached. That cached a list for
        the session without all the channels after that slot.

        Thus only ``NOT_FOUND`` (or a refusal without a code, from firmware that is too old
        to send a code) is a rejection. After any other refusal, MeshTerm asks again one
        time, because a borrowed error belongs to another command, and the next answer is
        the answer of this read. A second refusal, or no answer at all, is a failed read
        (:class:`DeviceCommandError`). The probe reports it as not finished, and does not
        cache it.
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
        # CMD_SET_TUNING_PARAMS has both floats ×1000. The firmware divides them again
        # (prefs.rx_delay_base = rx / 1000, prefs.airtime_factor = af / 1000).
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
            # The library sends only the bitmask. The hop limit is the byte after it.
            frame = bytes([_CMD_SET_AUTOADD_CONFIG, int(flags) & 0xFF, min(int(max_hops), 64)])
            self._ok(await mc.commands.send(frame, [EventType.OK, EventType.ERROR]))
        self._autoadd_hops = _UNREAD

    async def set_default_flood_scope(self, scope: str) -> None:  # noqa: D102
        from meshcore import EventType

        # MeshTerm builds the frame here, not through the library's
        # ``set_default_flood_scope``. That function stores the name *with* a ``#``. (The
        # firmware and the apps store it bare. Thus our name read back differently from
        # theirs, and a 30-byte name became a 31-byte name that the firmware refuses.) It
        # also pads by characters, not by UTF-8 bytes. (Thus a name with an accent put the
        # key at the wrong offset.) The frame is ``[63][name, 31 bytes NUL-padded]
        # [key16]``. A frame shorter than that clears the default.
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

        # MeshTerm builds the frame here, as for the default scope, not through the helpers
        # of the library. ``reset_flood_scope`` and ``force_unscoped`` came late in
        # meshcore 2.3.x. An install that satisfies ``meshcore>=2.3`` without them (2.3.7
        # on the uConsole) crashed each scoped channel send on the restore. The frame is
        # the firmware's own ``CMD_SET_FLOOD_SCOPE_KEY``. ``[54][0][key16]`` sets the
        # session scope, ``[54][0]`` alone clears it back to the default, and ``[54][1]``
        # forces unscoped.
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
        event = await self._require().commands.set_time(epoch)
        if getattr(event, "is_error", lambda: False)() and error_code(event) == _ERR_ILLEGAL_ARG:
            # The only argument that the set-time handler refuses: a time before the radio's
            # own clock, which the firmware never sets back (refer to ClockAheadError).
            raise ClockAheadError(None)
        self._ok(event)

    async def send_advert(self, flood: bool = False) -> None:  # noqa: D102
        transmit_gate.mark(flood_advert=flood)
        async with self.transmitting():
            self._ok(await self._require().commands.send_advert(flood))

    async def reboot(self) -> None:
        """Reboot the device, and return when the command is sent, not when it is acknowledged.

        The device sends no reply to a reboot. Over Bluetooth, it may not even acknowledge
        the write before it restarts (refer to :data:`_REBOOT_WRITE_GRACE_S`). Thus a wait
        for the write to complete only delays the reconnect flow by the link supervision
        timeout. A write that fails fast still raises. A write that is still pending after
        the grace time can complete, or end with the link, which the caller will tear down
        soon in any case.
        """
        write = asyncio.ensure_future(self._require().commands.reboot())
        done, _ = await asyncio.wait({write}, timeout=_REBOOT_WRITE_GRACE_S)
        if done:
            write.result()  # raise the error of a write that failed immediately
            return
        _REBOOT_WRITES.add(write)
        write.add_done_callback(_forget_reboot_write)

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
    """A deterministic simulator that implements the full :class:`Device` interface.

    The simulated SNR follows an inverted-U response to the TX power. If the power is too
    low, the signal is in the noise floor. If it is too high, the receiver saturates. Thus
    the TX optimizer gets a realistic curve, with one peak and noise, on which it can
    converge without hardware.

    Attributes:
        optimal_tx: The TX power at which the simulated link has its peak.
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
            seed: The RNG seed, for measurement noise that can be reproduced.
            optimal_tx: The TX power of our node at which the simulated *bottleneck* SNR
                has its peak.
            optimal_remote_tx: The TX power of a remote node at which the simulated SNR
                *at the target* has its peak (the value on which the remote-admin optimizer
                converges).
            admin_password: The password that the simulated remote nodes accept for the
                admin login.
        """
        self.optimal_tx = optimal_tx
        self.optimal_remote_tx = optimal_remote_tx
        self._admin_password = admin_password
        self._rng = random.Random(seed)
        self._tx_power = 20
        self._connected = False
        # The routes are the same as what real firmware learns from received floods. The
        # repeaters are direct neighbours, and the leaf nodes are one hop behind one of
        # them. Thus you can fully exercise the trace path composer and its topology
        # suggestions without a radio.
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
            # Last, so that each rotation over the first four (the synthetic traffic, and
            # the tests that depend on it) stays the same as before.
            Contact(
                name="Lakeside BBS",
                public_key=_mock_pub("f6a7b8c9"),
                key_prefix="f6a7b8c9",
                node_type=NODE_TYPE_ROOM,
                route_hops=("a1b2c3d4",),
            ),
        ]
        # The board of the simulated room server, oldest first, as (author key prefix,
        # posted at, text): the posts that Lakeside BBS has when the simulator starts. Its
        # authors are the three types that a room screen must draw: a contact (Alice,
        # Observer-Bot), a node that was never added (``e5f6a7b8``, a neighbour that no
        # contact names, so no name identifies it), and the room itself (a notice that its
        # admin posts with ``room.post`` arrives in this way). The board covers two days,
        # so the transcript has a day to divide.
        now = utcnow()
        self._room_board: list[tuple[str, datetime, str]] = [
            ("d4e5f6a7", now - timedelta(hours=26), "Anyone driving to the swap meet Saturday?"),
            (
                "c3d4e5f6",
                now - timedelta(hours=25),
                "I can take two. Leaving 8am from the IGA lot.",
            ),
            ("f6a7b8c9", now - timedelta(hours=3), "Reminder: this board keeps the last 32 posts."),
            (
                "e5f6a7b8",
                now - timedelta(minutes=50),
                "Is the north repeater down? Nothing since 6.",
            ),
            ("d4e5f6a7", now - timedelta(minutes=12), "Heard it an hour ago. Probably the solar."),
        ]
        #: Our access in each room where we have logged in, keyed by the mock key of the
        #: room. This is the access list of the room itself, for the part that concerns us.
        #: A room forgets its members when it restarts. The simulator never restarts a room.
        self._room_access: dict[str, RoomAccess] = {}
        #: The time of the newest post that we have from each room: the "sync since" of the
        #: companion. A login sends it, so that the room sends only newer posts.
        self._room_synced: dict[str, datetime] = {}
        #: The subscribers of the event stream now. Thus a room can push its posts to them
        #: after a login, in the same way as the catch-up of a real room arrives: unsolicited.
        self._listeners: list[EventCallback] = []
        #: Contacts whose acks arrive only after the send has stopped its wait. On a real
        #: mesh, this is the usual case for nodes farther than a neighbour. It is empty by
        #: default. Put a name in it to walk the late-ack path.
        self._late_acks: set[str] = set()
        #: The number of direct messages given to the device until now. Each message gets
        #: its ack code from this number.
        self._sent_codes = 0
        #: Nodes whose learned route has become old: a message sent along it gets no
        #: answer, and a flood gets through and learns a new route. It is empty by default.
        #: Put the name of a contact here to walk the "try by flood" path, which an old
        #: route gives to the user.
        self._stale_routes: set[str] = set()
        # Remote-admin simulation: the nodes where we are "logged in", and the transmit
        # power of each tuned node, keyed by its full public key. ``_default_remote_tx`` is
        # the assumed power before the optimizer writes a power for the first time.
        self._admin_sessions: set[str] = set()
        # Nodes for which the simulator answers *nothing*: the only failure that a password
        # cannot explain. It is empty by default, so the full simulated mesh can be reached
        # as before. Put the name of a contact here to walk the path of a repeater that is
        # down. There, a login comes back NO_REPLY, and the remembered credential must
        # survive it.
        self._unreachable: set[str] = set()
        self._remote_tx: dict[str, int] = {}
        self._default_remote_tx = 20
        # The settings of each simulated repeater, as its CLI shows them. MeshTerm fills
        # them with the defaults below at the first access (keyed by the full public key,
        # as for the TX map).
        self._remote_cfg: dict[str, dict[str, str]] = {}
        # Simulated neighbour tables, keyed by the key prefix of the repeater: what each
        # repeater "hears directly", as ``(neighbour_prefix, snr_db, secs_ago)``. The
        # ``e5f6a7b8`` entry is not in the contact list, on purpose. Thus the
        # fetched-evidence flow exercises the discovery of a node from which we never
        # received anything.
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
        # Simulated region tables, keyed as the neighbour tables are: one for each
        # repeater, which its ``region`` CLI edits and the anonymous regions request reads.
        # The table of Yagi fits in one reply. The table of Local is too big for the
        # 160-byte reply buffer on purpose, so you can drive the cut-and-recover path of
        # the region editor without hardware.
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
        # The settings state that can change, keyed exactly as the real SELF_INFO payload
        # is, so the settings registry operates the same on the simulator and on hardware.
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
            # 869.618, BW 62.5, SF8, and CR 5 by default). Thus the simulator boots with
            # the settings of a newly flashed companion, not with the 250 kHz / SF11
            # modulation that the EU mesh stopped using.
            "radio_freq": 869.618,
            "radio_bw": 62.5,
            "radio_sf": 8,
            "radio_cr": 5,
            "simulated": True,
        }
        self._tuning: dict = {"rx_delay": 0.0, "airtime_factor": 0.0}
        self._autoadd_config = 0
        self._autoadd_max_hops = 0
        # Client repeat (firmware v9+): off, as on a newly flashed companion.
        self._client_repeat = False
        self._flood_scope = ""
        #: The session send scope (``""`` follows the default, ``"*"`` is forced unscoped).
        self._send_scope = ""
        #: Each channel message given to the device: ``(slot, text, session scope at the time)``.
        self.sent_channel: list[tuple[int, str, str]] = []
        # Simulated clock skew (seconds behind the host), so the sync-clock flow has a
        # visible drift to correct until a call to set_time.
        self._clock_offset: int | None = -125
        self._path_hash_mode = 0
        self._custom_vars: dict[str, str] = {}
        self._channels: dict[int, dict] = {}
        # Model the fixed slot count of the firmware: the simulator rejects reads after the
        # last slot, exactly as a real device shows its limit (so ``channel_capacity``
        # finds 8 on the mock).
        self._max_channels = 8
        self._device_pin = 0
        self._private_key = "11" * 32
        # The background tasks that ``subscribe_events`` starts to make events. This set
        # keeps them, so they can be cancelled at the disconnect, and so they are never
        # garbage-collected while they are pending.
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
        # The simulator reports a stable model that is clearly synthetic. Thus the hardware
        # column renders the same as for a real board, but it does not claim to be one.
        # ``ble_pin`` is here, not in SELF_INFO, because real firmware puts it here.
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
        # Added with no learned route, exactly as the firmware stores a contact that it
        # got from the app, and did not hear.
        self._contacts.append(replace(node, route_hops=None))

    async def remove_contact(self, node: Contact) -> None:  # noqa: D102 - inherited docstring
        await asyncio.sleep(0)
        key = self._mock_key(node)
        # Firmware can delete only a contact that it has, and answers ERR_CODE_NOT_FOUND
        # for a contact that it does not have. The simulator refuses in the same way, so
        # you can walk the "not on the device, removed here in any case" path without a
        # radio.
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
        # Firmware can address only a contact that it has. Thus the simulator refuses a
        # recipient that this simulated device does not have, exactly as hardware refuses
        # it. With this, you can walk the "add it back and send" offer of the chat screen
        # on the simulator.
        if self._mock_key(contact) not in {self._mock_key(c) for c in self._contacts}:
            raise ContactNotOnDeviceError(contact)
        self._sent_codes += 1
        code = f"{self._sent_codes:08x}"
        held = next(c for c in self._contacts if self._mock_key(c) == self._mock_key(contact))
        flood = held.route_hops is None
        if contact.is_room:
            # A post. The room keeps it (and acknowledges it) only from a known member who
            # can post. The room ignores the posts of all other users, with no reply. That is
            # all that a read-only member learns about why a post went nowhere.
            access = self._room_access.get(self._mock_key(contact))
            if access is None or not access.can_post:
                return Delivery(code, None, flood)
            self._room_board.append((self._self_prefix(), utcnow(), text))
        if contact.name in self._late_acks:
            # Delivered, but the ack comes back slowly. It arrives after the wait, and the
            # simulator pushes it as the radio pushes an ack when it arrives.
            self._push_ack_later(code)
            return Delivery(code, None, flood)
        # In other cases, the simulator "delivers" immediately and always acknowledges, so
        # sent direct messages show as acked without a radio.
        return Delivery(code, Ack(code=code), flood)

    def _push_ack_later(self, code: str) -> None:
        """Push an ack to all listeners, a short time after the send returned without one."""

        async def push() -> None:
            await asyncio.sleep(_MOCK_MONITOR_INTERVAL_S)
            for listener in list(self._listeners):
                listener(MeshEvent.ack_event(Ack(code=code)))

        task = asyncio.create_task(push())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def send_channel_message(  # noqa: D102 - inherited docstring
        self, index: int, text: str
    ) -> None:
        transmit_gate.mark()
        async with self.transmitting():
            await asyncio.sleep(0)
            #: The scope of each channel message: the session scope at the moment when it
            #: was given to the device (``""`` the default, ``"*"`` forced unscoped).
            self.sent_channel.append((index, text, self._send_scope))

    async def admin_login(self, node: Contact, password: str) -> LoginResult:  # noqa: D102
        await asyncio.sleep(0)
        if node.is_room:
            # One login, whatever screen sends it. An admin login to a room makes us a member
            # to which the room pushes posts. The rules of the room decide the answer (also
            # its silence for a wrong password), exactly as the rules of the firmware do.
            return (await self.room_login(node, password)).result
        if node.name in self._unreachable:
            return LoginResult.NO_REPLY  # simulates a node that is down or out of range
        if password != self._admin_password:
            return LoginResult.REFUSED
        self._admin_sessions.add(self._mock_key(node))
        return LoginResult.ACCEPTED

    async def room_login(self, room: Contact, password: str) -> RoomLogin:  # noqa: D102
        await asyncio.sleep(0)
        key = self._mock_key(room)
        held = next((c for c in self._contacts if self._mock_key(c) == key), None)
        if held is None:
            return RoomLogin(
                LoginResult.NO_REPLY,
                flood=None,
                radio_error="this node isn't in the radio's contacts",
            )
        # How the radio sends it: along its learned route, or by flood when the route is gone.
        flood = held.route_hops is None
        if room.name in self._unreachable or (room.name in self._stale_routes and not flood):
            return RoomLogin(LoginResult.NO_REPLY, flood=flood)
        if flood:
            # The answer to a flood brings a new route back with it, as on hardware.
            self._stale_routes.discard(room.name)
            self._set_route(key, self._LEARNED_ROUTE)
        cfg = self._remote_config(room)
        if password == self._admin_password:
            access = RoomAccess.ADMIN
        elif not password and key in self._room_access:
            access = self._room_access[key]  # "do you know me?": it does
        elif password == cfg["guest.password"]:
            access = RoomAccess.MEMBER
        elif cfg["allow.read.only"] == "on":
            access = RoomAccess.READ_ONLY
        else:
            # A room never says no: a wrong password gets silence.
            return RoomLogin(LoginResult.NO_REPLY, flood=flood)
        self._room_access[key] = access
        if access is RoomAccess.ADMIN:
            self._admin_sessions.add(key)
        self._push_room(room)
        return RoomLogin(LoginResult.ACCEPTED, access, flood=flood)

    async def reset_route(self, node: Contact) -> None:  # noqa: D102 - inherited docstring
        await asyncio.sleep(0)
        key = self._mock_key(node)
        if not any(self._mock_key(c) == key for c in self._contacts):
            raise ContactNotOnDeviceError(node)
        self._set_route(key, None)

    #: The route that the simulator "learns" from the answer to a flood: one hop, through Yagi.
    _LEARNED_ROUTE = ("a1b2c3d4",)

    def _set_route(self, key: str, hops: tuple[str, ...] | None) -> None:
        """Give the contact with ``key`` a learned route, or forget its route (``None``)."""
        self._contacts = [
            replace(c, route_hops=hops) if self._mock_key(c) == key else c for c in self._contacts
        ]

    def _push_room(self, room: Contact) -> None:
        """Start the catch-up of a room: the posts after our newest post, oldest first.

        A real room sends one post, waits for the acknowledgement of the companion, and then
        sends the next post. The simulator spaces the posts by its event interval, so a room
        screen fills visibly, and not all at the same time. The room never sends our own
        posts back to us.
        """
        key = self._mock_key(room)
        since = self._room_synced.get(key)
        own = self._self_prefix()
        pending = [
            post
            for post in self._room_board
            if post[0] != own and (since is None or post[1] > since)
        ]
        if not pending:
            return

        async def push() -> None:
            for author, posted_at, text in pending:
                await asyncio.sleep(_MOCK_MONITOR_INTERVAL_S)
                self._room_synced[key] = posted_at
                message = Message(
                    text=text,
                    sender=room.key_prefix,
                    sender_timestamp=posted_at,
                    snr=round(self._rng.gauss(6.0, 3.0), 1),
                    author=author,
                )
                for listener in list(self._listeners):
                    listener(MeshEvent.message_event(message))

        task = asyncio.create_task(push())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _self_prefix(self) -> str:
        """The four-byte key prefix of our node: the way a room signs a post that we wrote."""
        return str(self._info.get("public_key") or "")[:8].lower()

    def _remote_config(self, node: Contact) -> dict[str, str]:
        """The settings of a simulated node, as its CLI shows them, with defaults at first access.

        A room server starts with the stock room password that its build sets (MeshCore's
        ``ROOM_PASSWORD``, ``hello``), and with forwarding off, as a new room has. The
        other values are the repeater defaults, which the two firmwares share.
        """
        key = self._mock_key(node)
        cfg = self._remote_cfg.get(key)
        if cfg is None:
            cfg = {"name": node.name, **self._REMOTE_CFG_DEFAULTS}
            if node.is_room:
                cfg.update({"guest.password": self._ROOM_PASSWORD, "repeat": "off"})
            self._remote_cfg[key] = cfg
        return cfg

    #: The stock room password with which a MeshCore room server is built.
    _ROOM_PASSWORD = "hello"

    #: The settings of the simulated repeater CLI, keyed and answered as MeshCore's
    #: ``CommonCLI.cpp`` does (refer to send_remote_command). The values that are absent
    #: are absent on purpose: a board with no front-end module, and a build with no
    #: bridge. Thus a read of those values answers ``??:``, exactly as that hardware would.
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

    #: The ``get radio`` fields, in the order in which the reply joins them.
    _REMOTE_RADIO = ("freq", "bw", "sf", "cr")

    async def send_remote_command(  # noqa: D102 - inherited docstring
        self, node: Contact, command: str, *, timeout: float = 8.0
    ) -> str | None:
        transmit_gate.mark()
        await asyncio.sleep(0)
        key = self._mock_key(node)
        if key not in self._admin_sessions:
            return None  # firmware ignores unknown nodes: a timeout, as on hardware
        cfg = self._remote_config(node)
        parts = command.strip().split()
        verb = parts[0].lower() if parts else ""
        if verb == "ver":
            return "MeshCore v1.15.0 (simulator)"
        if verb == "room.post" and node.is_room:
            # A notice that the admin posts in the name of the room (MeshCore's ``addSystemPost``).
            text = command.strip()[len("room.post") :].strip()
            if not text:
                return "ERR empty message"
            self._room_board.append((key[:8], utcnow(), text))
            return "OK"
        if verb == "clock":
            return "OK - clock synced" if parts[1:] == ["sync"] else "12:00 - 1/1/2026 UTC"
        if verb == "advert":
            return "OK - Advert sent"
        regions = self._region_maps.get(node.key_prefix or "")
        if verb == "region" and regions is not None:
            return regions.command(command.strip())
        if verb == "reboot" and regions is not None:
            regions.reboot()  # a power cycle loses each region edit that was not saved
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
            # freq alone is serial-only. The other radio values go through "set radio".
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
        # The same as real firmware (checked on v1.15): without a login, the firmware
        # silently ignores the request, and the caller gets a timeout.
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
        """Return the lookup key for a remote node (its public key, or else its key prefix)."""
        return (node.public_key or node.key_prefix or node.name).lower().removeprefix("0x")

    def _remote_tx_for(self, hop_hex: str) -> int | None:
        """Find the simulated remote TX power set on a forced-path hop, if there is one.

        The optimizer stores the power of a node under its full public key. A trace
        addresses the node by a shorter hash prefix, so match in both directions.

        Args:
            hop_hex: The hash of the forced-path hop (hex).

        Returns:
            The simulated TX power of the node, or ``None`` if we never set one (that is,
            this hop is not a node that the optimizer tunes).
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
        # Modelled on the firmware: the relay is refused on frequencies that are not
        # accepted, and a command without the repeat byte turns the relay *off*. A radio
        # change must state the value again to avoid this trap.
        if repeat and not repeat_freq_allowed(freq, _DEFAULT_REPEAT_FREQS):
            raise DeviceCommandError("device rejected the command: illegal argument")
        self._info.update(radio_freq=freq, radio_bw=bw, radio_sf=sf, radio_cr=cr)
        self._client_repeat = bool(repeat)

    async def set_tuning(self, rx_delay: float, airtime_factor: float) -> None:  # noqa: D102
        # Do a round trip through the ×1000 integer scaling of the protocol, so the
        # simulator loses precision exactly where real firmware would.
        self._tuning = {
            "rx_delay": round(float(rx_delay) * 1000) / 1000.0,
            "airtime_factor": round(float(airtime_factor) * 1000) / 1000.0,
        }

    async def set_autoadd_config(  # noqa: D102 - inherited docstring
        self, flags: int, max_hops: int | None = None
    ) -> None:
        self._autoadd_config = int(flags)
        if max_hops is not None:  # the firmware does not change the limit when it is not sent
            self._autoadd_max_hops = min(int(max_hops), 64)

    async def set_default_flood_scope(self, scope: str) -> None:  # noqa: D102
        # Do as the firmware does: empty (or the wildcard) clears, and a name is stored bare.
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

        # As in the firmware, the simulated clock moves only forward.
        current = await self.get_time()
        if current is not None and epoch < current:
            raise ClockAheadError(None)
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
        self._private_key = bytes.fromhex(key_hex).hex()  # checks the hex, and normalizes it

    async def factory_reset(self) -> None:  # noqa: D102 - inherited docstring
        self._custom_vars.clear()
        self._channels.clear()

    async def subscribe_events(  # noqa: D102 - inherited docstring
        self, on_event: EventCallback
    ) -> Unsubscribe:
        # Simulate a live event stream. Make a burst of synthetic adverts and telemetry from
        # the known contacts *synchronously* here. Then continue to make bursts at a steady
        # interval from a background task, until the unsubscribe. Because of the immediate
        # first burst, also a time window of zero length always gets each contact (two of
        # them have a location) and one received message. This keeps capture tests
        # deterministic.
        stop = asyncio.Event()
        seq = 0
        burst = 0

        def emit_burst() -> None:
            nonlocal seq, burst
            for contact in self._contacts:
                on_event(MeshEvent.observation_event(self._synth_observation(contact, seq)))
                seq += 1
            # Simulate the RX packet log of the companion: overheard packets from the leaf
            # nodes that have a route, each with the relay path that it went through. Thus
            # the topology capture collects passive path evidence on the simulator, exactly
            # as on hardware with packet logging on. The first burst always has one.
            if burst % 4 == 0:
                on_event(MeshEvent.observation_event(self._synth_packet(burst // 4)))
            # At intervals, simulate a received direct message, so the features that react
            # to messages (and their tests) have traffic. The first burst always has one,
            # so a subscriber gets a message without a wait.
            if burst % 8 == 0:
                on_event(MeshEvent.message_event(self._synth_message(burst // 8)))
            burst += 1

        emit_burst()

        async def emit_loop() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), _MOCK_MONITOR_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass  # the interval ended: make the next burst
                if not stop.is_set():
                    emit_burst()

        task = asyncio.create_task(emit_loop())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        self._listeners.append(on_event)

        def unsubscribe() -> None:
            stop.set()  # wakes the loop at once, and it exits at the next check
            if on_event in self._listeners:
                self._listeners.remove(on_event)

        return unsubscribe

    #: The fixed locations that the two simulated repeaters advertise. Thus the features
    #: that use locations (for example, the coverage map) always have coordinates.
    _MOCK_LOCATIONS = {
        "Yagi-Repeater": (45.5019, -73.5674),
        "Local-Repeater": (45.4768, -73.5990),
    }

    def _synth_observation(self, contact: Contact, seq: int) -> Observation:
        """Build one plausible synthetic observation for ``contact`` (simulator only).

        Args:
            contact: The contact from which to synthesize a reception.
            seq: A monotonic counter of the events, used to change the packet kind.

        Returns:
            A noisy :class:`Observation`, tagged ``telemetry`` on each fourth packet and
            ``advert`` on the others, with a location for the simulated repeaters.
        """
        lat_lon = self._MOCK_LOCATIONS.get(contact.name)
        # Each node advertises the type that its contact has (the two repeaters with a
        # location, the companions, the room server). Thus the map and the lists have each
        # class to draw.
        node_type = contact.node_type or NODE_TYPE_CHAT
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
        """Build one plausible packet observation from the RX log (simulator only).

        It rotates over the leaf contacts that are behind a repeater, and makes the packet
        with the relay path that its route implies. This is the same as how a real
        companion reports an overheard relayed packet.

        Args:
            seq: A monotonic counter of the events, used to rotate the contact of origin.

        Returns:
            A ``packet``-kind :class:`Observation` with a one-hop relay path.
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
        """Build one plausible synthetic received direct message (simulator only).

        Args:
            seq: A monotonic counter of the bursts, used to rotate the sender and the body.

        Returns:
            A :class:`Message` from one of the known contacts.
        """
        # A room server sends posts, never a message of its own (refer to _push_room).
        senders = [c for c in self._contacts if not c.is_room]
        contact = senders[seq % len(senders)]
        return Message(
            text=f"hello from {contact.name} #{seq}",
            sender=contact.key_prefix or contact.public_key[:12],
            is_channel=False,
            snr=round(self._rng.gauss(6.0, 3.0), 1),
        )

    def _expected_snr(self, hop_index: int) -> float:
        """Model the SNR for a hop as an inverted-U in TX power, with a decrease over distance.

        Args:
            hop_index: The zero-based position of the hop. Deeper hops are weaker.

        Returns:
            The expected SNR in dB without noise, for the current TX power.
        """
        # An inverted parabola with its peak at ``optimal_tx``. The SNR changes by
        # approximately 10 dB across the range.
        span = (TX_POWER_MAX - TX_POWER_MIN) / 2
        offset = (self._tx_power - self.optimal_tx) / span
        peak = 8.0 - 10.0 * (offset**2)
        return peak - 2.5 * hop_index

    def _expected_remote_snr(self, remote_tx: int) -> float:
        """Model the SNR that the target receives from the admin node that it is behind.

        The model is an inverted-U in the TX power of the admin node. If the power is too
        low, the target only just hears it. If it is too high, the front end of the target
        saturates. The peak is at ``optimal_remote_tx``, so the remote-admin optimizer has
        a curve with one peak and noise, on which it can converge.

        Args:
            remote_tx: The transmit power of the admin node.

        Returns:
            The expected SNR in dB at the target, without noise.
        """
        # The curve is steep enough that the band edges go below the drop threshold of
        # approximately -12 dB. Thus traces start to fail there. This gives the optimizer a
        # real reliability gradient (not only an SNR gradient), so it can put reliability
        # first.
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
        await asyncio.sleep(0.05)  # simulate the radio latency, so progress bars are visible
        forced = [h for h in path.split(",") if h.strip()] if path else None
        # As on the real device: the path-hash width is the byte length of a forced hop.
        hash_bytes = len(bytes.fromhex(forced[0])) if forced else None
        depth = len(forced) if forced else self._rng.randint(1, 3)
        hops: list[Hop] = []
        for i in range(depth):
            # The SNR of each hop shows the node that transmitted *into* it (hop i-1). If
            # the optimizer has tuned a remote TX on that node, model the link from its
            # power. Thus the hop that arrives at the target follows the admin node that we
            # tune, wherever the target is in an out-and-back path. If not, use the
            # TX-power model of our node.
            prev_tx = self._remote_tx_for(forced[i - 1]) if forced is not None and i >= 1 else None
            if prev_tx is not None:
                expected = self._expected_remote_snr(prev_tx)
            else:
                expected = self._expected_snr(i)
            snr = expected + self._rng.gauss(0, 1.2)  # measurement noise
            node = forced[i] if forced else f"hop{i}"
            hops.append(Hop(index=i, node=node, snr=round(snr, 1)))

        # Very weak links sometimes lose the full trace, as judged on the bottleneck hop.
        bottleneck = min((h.snr for h in hops), default=-99)
        success = bottleneck > -12 or self._rng.random() > 0.1

        # The trace reply comes back to us: the firmware adds our device as a last hop
        # without a hash (``node=None``). Do the same, so that our device is the origin
        # and the destination, at both ends of the path. The return link is modelled as
        # symmetric to the first outbound hop, so it never changes the bottleneck SNR.
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
    """Get the SNR of each hop from a ``TRACE_DATA`` event payload.

    meshcore parses a trace reply into ``payload["path"]``: a list of nodes. Each node is
    a dict with a repeater ``"hash"`` and its ``"snr"`` (already in dB: the signed byte /
    4). The last node is our device, and it has an ``"snr"`` but no ``"hash"``.

    Args:
        payload: The ``TRACE_DATA`` event payload.

    Returns:
        The hops in path order. The function skips nodes without an SNR reading.
    """
    hops: list[Hop] = []
    for node in payload.get("path") or []:
        if not isinstance(node, dict) or node.get("snr") is None:
            continue
        hops.append(Hop(index=len(hops), node=node.get("hash"), snr=float(node["snr"])))
    return hops


def observation_from_event(event, kind: str) -> Observation | None:  # noqa: ANN001
    """Map a meshcore advert or telemetry event into an :class:`Observation`.

    The companion reports a node identifier, optionally a name and a shared location, and
    the SNR and RSSI of the reception. The field names change across firmware and library
    versions, so the function tries several usual names for each value. As for the trace
    mapping, this is best-effort, and you must check it with the event schema of your
    firmware.

    Args:
        event: A meshcore event (anything that has a ``payload`` mapping).
        kind: The observation class with which to tag the record (for example
            ``advert``).

    Returns:
        The parsed :class:`Observation`, or ``None`` if the payload had no node id.
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
    node = ident[:12]  # the stored canonical id of 12 hex digits (all groups and joins use it)
    # Keep the full key when the advert had one (public_key or pubkey), so a key lane can
    # show more than the twelve stored digits later. An advert with only a key prefix
    # leaves it None.
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

    The RX packet log of the companion reports each packet that it overhears, with the
    relay path of the header: the repeaters that the packet went through before it
    reached us, the one nearest to the originator first. That path is the passive topology
    evidence that the trace path composer uses, so the function keeps it exactly (as
    comma-separated hex for each hop).

    The originating node can be known only when the payload class shows it. The library
    decodes adverts inline (``adv_key``/``adv_name``), so adverts have an origin. Other
    packet classes are stored without an origin. Their path (and our reception of its
    last relay) is still adjacency evidence.

    A packet that went through no relays at all is also kept, with an empty path. It is
    not without information: it is the strongest adjacency evidence that there is,
    because we heard the transmitter directly. It is also all that a nearby device
    transmits. Thus, when MeshTerm discarded it, the traffic of a neighbour was not
    visible. Only a packet to which the library could not even give a payload class is
    removed.

    One class must have a different read of its header: the ``path`` field of a ``TRACE`` has
    the SNR readings of each hop, not relay hashes (refer to
    :func:`~meshterm.core.frames.trace_link_snrs`). Thus it gives no hops, and its
    readings are merged in as ``trace_snrs`` instead.

    But a packet without an origin is not a packet without features. The function decodes
    what the packet *addresses* from its undecoded body
    (:func:`~meshterm.core.frames.frame_addressing`), and merges it into the raw payload:
    the recipient and sender hashes of a direct message or a request, the full sender key
    of an anonymous request, the envelope of a channel datagram, the checksum of an ack,
    the tag of a trace. Thus each class has something to say about itself to later code.

    Args:
        event: A meshcore ``RX_LOG_DATA`` event (anything that has a ``payload`` mapping).

    Returns:
        The parsed :class:`Observation` (``kind="packet"``), or ``None`` for a packet with
        no payload class that can be decoded, or with a path that cannot be parsed.
    """
    payload = dict(getattr(event, "payload", {}) or {})
    typename = payload.get("payload_typename")
    if not typename or typename == "UNK":
        return None  # the sentinel of the library for a packet too short to have a class
    path_len = _as_int(payload.get("path_len")) or 0
    hash_size = _as_int(payload.get("path_hash_size")) or 1
    path_hex = str(payload.get("path") or "").lower().removeprefix("0x")
    hops: list[str] = []
    # The path field of a trace has SNR readings, not relay hashes (refer to
    # :func:`~meshterm.core.frames.trace_link_snrs`). Thus it gives no hops. The code
    # below gets its readings back, and keeps them with the packet instead.
    if path_len > 0 and typename != "TRACE":
        width = hash_size * 2
        hops = [path_hex[i * width : (i + 1) * width] for i in range(path_len)]
        if any(len(h) != width for h in hops):
            return None  # a truncated path would make a false adjacency between wrong nodes

    origin = payload.get("adv_key")
    # A packet with no origin and no relays is not without features. It is the strongest
    # adjacency evidence that the mesh gives: we heard the transmitter *directly*, with
    # nothing between. When MeshTerm removed such packets, each packet that was not an
    # advert from a nearby node was not visible. That is why a companion next to this one
    # could trace all day and never appear in the feed. The data of the packet (its
    # class, what it addresses, how well it was heard) is stored exactly as for a relayed
    # packet, with an empty path for the zero hops that it went through.
    #
    # What the packet addresses (the recipient, the sender, the channel, the token that
    # it has) is read from the body. The library does not decode the body for any class
    # except advert and channel text (refer to :mod:`~meshterm.core.frames`). These values
    # are merged in under their own keys. Thus a class that names no origin node still
    # tells what it is *about*.
    payload.update(frame_addressing(payload))
    readings = trace_link_snrs(payload)
    if readings is not None:
        # Kept with the packet, not in ``path``: the hop readings of a trace tell how well
        # each leg was heard, and nothing at all about which node relayed it.
        payload["trace_snrs"] = readings
    ident = str(origin).lower().removeprefix("0x") if origin else None
    node = ident[:12] if ident else None
    public_key = ident if ident and len(ident) > 12 else None  # keep the full adv_key
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
    """Map a meshcore ``CONTACT_MSG_RECV`` or ``CHANNEL_MSG_RECV`` event into a message.

    Direct messages have a ``pubkey_prefix`` sender. Channel messages have a
    ``channel_idx`` instead (``type`` is ``"PRIV"`` or ``"CHAN"``). The field names are
    best-effort, and you must check them with the event schema of your firmware.

    A room post is a direct message from the room server, of the *signed* text type
    (:data:`_TXT_TYPE_SIGNED_PLAIN`). The library gives the four bytes with which the room
    signs it as ``signature``. This is not a cryptographic signature, but the first four
    bytes of the public key of the author (MeshCore's ``pushPostToClient``). These bytes
    become :attr:`~meshterm.core.models.Message.author`.

    Args:
        event: A meshcore message event (anything that has a ``payload`` mapping).

    Returns:
        The parsed :class:`Message`, or ``None`` if the payload had no text body.
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
    author = None
    if not is_channel and payload.get("txt_type") == _TXT_TYPE_SIGNED_PLAIN:
        author = str(payload.get("signature") or "").lower() or None
    return Message(
        text=str(text),
        sender=None if is_channel else payload.get("pubkey_prefix"),
        channel=payload.get("channel_idx") if is_channel else None,
        is_channel=is_channel,
        sender_timestamp=sender_ts,
        snr=_as_float(payload.get("SNR", payload.get("snr"))),
        author=author,
        raw=payload,
    )


def ack_from_event(event) -> Ack:  # noqa: ANN001
    """Map a meshcore ``ACK`` event into an :class:`Ack` (delivery acknowledgement)."""
    payload = dict(getattr(event, "payload", {}) or {})
    return Ack(code=payload.get("code"), raw=payload)


def _as_float(value: object) -> float | None:
    """Best-effort float conversion. It returns ``None`` for missing or bad values."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    """Best-effort int conversion. It returns ``None`` for missing or bad values."""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _contact_route(info: dict) -> tuple[str, ...] | None:
    """Get the outbound route of a contact as the device learned it, one hex hash per hop.

    The firmware reduces the paths of received flood packets into the ``out_path`` of
    each contact: the repeater chain to send through, one path hash for each hop, from us
    outward. The protocol reports the hop count and the hash width packed into one byte
    (``0xFF`` = no learned route, that is, flood), and the path itself as a fixed 64-byte
    field. Thus the real route is the leading ``out_path_len × size`` bytes.

    Args:
        info: The raw info mapping of one contact, from the contacts payload of the
            companion.

    Returns:
        The route as a tuple of hex hashes, one for each hop (empty = a learned *direct*
        route), or ``None`` when no route is learned or the report cannot be parsed.
    """
    out_path_len = _as_int(info.get("out_path_len"))
    if out_path_len is None or out_path_len < 0:
        return None  # 0xFF in the protocol: flood routing, no learned path
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
        return None  # the field was shorter than the declared route: do not guess
    return hops


def _contact_location(info: dict) -> tuple[float | None, float | None]:
    """Get the advertised ``(lat, lon)`` of a contact, or ``(None, None)`` if it has none.

    A node that has never set coordinates advertises ``0.0/0.0`` (null island), and the
    firmware reports this value without a change. We treat that as "no location", instead
    of a point in the Gulf of Guinea on the map.

    Args:
        info: The raw info mapping of one contact, from the contacts payload of the
            companion.

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
        prefix: The leading hex digits that identify the node.

    Returns:
        A key of 64 hex characters (32 bytes) that starts with ``prefix``.
    """
    return prefix + "0" * (64 - len(prefix))


def _parse_tx_reply(reply: str | None) -> int | None:
    """Get a TX-power integer from the ``get tx`` reply text of a repeater.

    Repeater firmware answers briefly, and differently across versions (for example
    ``"tx: 20"``, ``"TX power = 20 dBm"``, or only ``"20"``). Thus get the first signed
    integer from the reply, and do not try to match a fixed format.

    Args:
        reply: The reply text of the node, or ``None`` if it did not answer.

    Returns:
        The parsed TX power, or ``None`` if the reply was empty or had no number.
    """
    if not reply:
        return None
    import re

    match = re.search(r"-?\d+", reply)
    return int(match.group()) if match else None


def clamp_tx_power(value: int) -> int:
    """Clamp a TX power value to the supported range.

    Args:
        value: The requested TX power level.

    Returns:
        ``value``, limited to ``[TX_POWER_MIN, TX_POWER_MAX]``.
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
    """Make the correct :class:`Device` for the current run.

    Args:
        mock: When ``True``, return a :class:`MockDevice` simulator.
        port: The serial port for a real serial device. It is necessary for the serial
            transport, unless ``mock`` is set.
        baudrate: The serial baud rate for a real serial device.
        mock_optimal_tx: The peak TX power for the simulator.
        transport: ``"serial"`` (default), ``"ble"``, ``"tcp"``, or ``"spi"``.
        address: The Bluetooth address for the BLE transport. It is necessary when
            ``transport="ble"``.
        pin: An optional BLE pairing PIN (BLE only).
        ble_device: The scanned ``bleak.BLEDevice`` for ``address``, when the discovery of
            this session found one (BLE only). With it, the connect does not discover the
            peripheral again by address (refer to :class:`MeshCoreDevice`).
        host: The host name or IP address for the TCP transport. It is necessary when
            ``transport="tcp"``.
        tcp_port: The TCP port for the TCP transport. It is necessary when
            ``transport="tcp"``.
        spi: How the radio is wired, for the SPI transport. It is necessary when
            ``transport="spi"``.
        state: The state directory of the SPI radio (refer to
            :func:`meshterm.core.spiradio.state_dir`). It is necessary when
            ``transport="spi"``.

    Returns:
        A :class:`Device` instance that connects when you enter it.

    Raises:
        ValueError: If a real device is requested without an endpoint that its transport
            can use.
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


#: The handshake window (seconds) for a probe of a serial companion. A real board answers in
#: much less than one second. MeshTerm rejects a port that is not MeshCore within this limit.
_PROBE_TIMEOUT_SERIAL_S = 6.0

#: The handshake window (seconds) for a probe of a BLE companion. It is longer than for serial,
#: because a BLE connect has a link-layer connection and a GATT service discovery before the
#: identity reply.
_PROBE_TIMEOUT_BLE_S = 20.0

#: The handshake window (seconds) for a probe of a TCP companion. It is between serial and
#: BLE. A TCP connect is a fast socket open, but a host that cannot be reached can wait in
#: the connect backoff of the OS. The window gives time for that, before MeshTerm considers
#: the endpoint as absent.
_PROBE_TIMEOUT_TCP_S = 10.0


async def probe_device(
    device: DiscoveredDevice,
    *,
    baudrate: int = 115200,
    pin: str | None = None,
    spi_state: Path | None = None,
) -> tuple[MeshCoreDevice, dict] | None:
    """Open a discovered device, confirm that a MeshCore companion answers, and keep it connected.

    This is the entry for the startup smoke test, and it is the same for all transports. It
    opens the correct connection for ``device`` (serial port or BLE address), and sends an
    identity query (the APPSTART that :meth:`MeshCoreDevice.get_self_info` uses). A real
    companion replies with a self-info payload. Anything else (a device that is not
    MeshCore, a port that does not respond, a BLE device out of range) never answers, and
    MeshTerm rejects it within the timeout of the transport.

    On success, the probe **does not close** the connection, and returns it to the caller,
    which uses it again as the session device. This is on purpose. Many companion boards
    reset at each serial open (and a BLE reconnect runs the service discovery again). Thus a
    cycle of probe and open again is slow and not reliable. When MeshTerm opens the radio
    exactly one time, it is faster and much more reliable. On any failure, the probe closes
    the connection.

    Args:
        device: The discovered device to probe (serial or BLE).
        baudrate: The serial baud rate (serial transport only).
        pin: An optional BLE pairing PIN (BLE transport only).
        spi_state: The state directory of the radio (SPI transport only). A probe of an SPI
            radio starts its node, and the node must have a place to keep its identity.

    Returns:
        ``(device, self_info)`` with a connected :class:`MeshCoreDevice` on success (the
        caller owns it, and must close it at some time), or ``None`` if it is not a
        MeshCore companion that MeshTerm can reach.

    Raises:
        DeviceCommandError: On a failure that the user can repair (for example, a Bluetooth
            companion that must have a pairing PIN). Thus the caller can show the repair,
            instead of an unhelpful "didn't answer".
    """
    if device.is_ble:
        timeout = _PROBE_TIMEOUT_BLE_S
        probe = MeshCoreDevice(
            transport="ble",
            address=device.address,
            pin=pin,
            connect_timeout=timeout,
            # The scan that discovered the device already has its BLEDevice. A connect
            # through it skips the discovery again by address, which makes BLE startups
            # unreliable.
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
        # The window covers the start of the node, not only the handshake. The import of
        # the radio library and the bring-up of the chip come first, and a board this small
        # is slow at both.
        timeout = READY_TIMEOUT_S
        probe = SpiRadioDevice(device.spi or SpiWiring(), spi_state)  # type: ignore[arg-type]
    else:
        timeout = _PROBE_TIMEOUT_SERIAL_S
        probe = MeshCoreDevice(port=device.port, baudrate=baudrate, connect_timeout=timeout)
    return await _probe(probe, timeout)


async def probe_meshcore(
    port: str, baudrate: int = 115200, *, timeout: float = _PROBE_TIMEOUT_SERIAL_S
) -> tuple[MeshCoreDevice, dict] | None:
    """Probe a serial ``port`` for a MeshCore companion (refer to :func:`probe_device`).

    This is a thin wrapper, only for serial, kept for callers that have a bare port string.

    Args:
        port: The serial port to probe (for example ``COM5`` or ``/dev/ttyUSB0``).
        baudrate: The serial baud rate.
        timeout: The time limit in seconds for the connection handshake and the identity
            reply.

    Returns:
        ``(device, self_info)`` on success, or ``None`` if the port is not a MeshCore
        companion that MeshTerm can reach.
    """
    return await _probe(
        MeshCoreDevice(port=port, baudrate=baudrate, connect_timeout=timeout), timeout
    )


async def _probe(device: MeshCoreDevice, timeout: float) -> tuple[MeshCoreDevice, dict] | None:
    """Connect ``device`` and read its identity. Return it live, or close it on a failure.

    Args:
        device: A :class:`MeshCoreDevice` that is not connected, set up for its transport.
        timeout: The handshake window, which limits both the connect and the identity read.

    Returns:
        ``(device, self_info)`` with the connection open, or ``None`` if the endpoint is
        not a MeshCore companion that MeshTerm can reach.

    Raises:
        DeviceCommandError: On a failure that the user can repair (for example, a Bluetooth
            companion that must have a pairing PIN). This is different from ``None`` on purpose
            (a usual "not a companion" miss), so the caller can show the real repair instead
            of a generic "didn't answer". A failure with no name of its own comes as
            :class:`UnrecognisedConnectError`, in the words of the original error.
    """
    # ``timeout`` is the handshake window that the client gets. Thus the client rejects an
    # endpoint that is not MeshCore in approximately ``timeout`` seconds, and cleans up its
    # own connection. The outer ``wait_for`` is only a safety net, a few seconds after that.
    # Thus we never cancel the client during the handshake (that would leak the open
    # connection). A real companion answers in much less than one second (serial) or in a
    # few seconds (BLE), so this never delays a good device.
    stage = "connect"
    try:
        await asyncio.wait_for(device.connect(), timeout + 4.0)
        stage = "identity"
        info = await asyncio.wait_for(device.get_self_info(), timeout)
    except asyncio.TimeoutError as exc:
        await _safe_disconnect(device)
        transport = getattr(device, "transport", None)
        if transport == "tcp" and stage == "connect":
            # The OS gives a silent host twenty seconds or more before it stops. Thus this
            # window ends first, and it has the same meaning as the timeout of the OS.
            where = getattr(device, "_host", None) or "the host"
            _log.warning("TCP probe of %s timed out connecting", device.endpoint)
            message = _tcp_open_message(where, getattr(device, "_tcp_port", None), exc)
            raise DeviceCommandError(message or str(exc)) from exc
        if transport != "ble":
            return None
        # A Bluetooth connect has more stages than a serial connect (link, pairing, service
        # discovery, the identity reply). When the message tells which stage used all its
        # time, the user can tell a device out of range from a device that connected and
        # then went silent.
        where = device.endpoint or "the selected Bluetooth device"
        if stage == "connect":
            message = _ble_stalled_message(where, timeout + 4.0)
        else:
            message = (
                f"connected to {where} over Bluetooth, but it didn't answer the identity query "
                f"within {timeout:.0f}s — it may be busy with another app; restart it and try "
                "again."
            )
        _log.warning("BLE probe of %s timed out at the %s stage", where, stage)
        raise DeviceCommandError(message) from exc
    except DeviceCommandError:
        # A clean failure that the user can act on (for example, a PIN is necessary).
        # Close the probe and let the error through, so the picker shows the repair, and
        # does not hide it behind "didn't answer".
        await _safe_disconnect(device)
        raise
    except Exception as exc:  # noqa: BLE001 - said in words, logged whole
        # A failure that no step above recognized. In the past, it came back as "not
        # confirmed", and the picker said that the device
        # "didn't answer as a MeshCore device". The probe had no evidence for this claim,
        # and the only clue stayed in the log. Now the message has the words of the error
        # itself, and the log has the traceback.
        await _safe_disconnect(device)
        where = getattr(device, "endpoint", None) or "the device"
        what = (
            f"couldn't connect to {where}"
            if stage == "connect"
            else f"connected to {where}, but reading its identity failed"
        )
        raise _unrecognised(what, exc) from exc
    if not info:
        await _safe_disconnect(device)
        return None
    # The connection is already open, so get the hardware model now (with its own protocol
    # query), and merge it into the identity dict. This is the only time when the model is
    # available, and it lets the caller remember "Seeed Tracker T1000-E" without a second
    # connect. It only adds data: the self-info fields win on the keys (which do not overlap
    # now), and a firmware that cannot answer the query gives nothing.
    try:
        device_info = await asyncio.wait_for(device.get_device_info(), timeout)
    except Exception:  # noqa: BLE001 - the model is optional. A good probe never fails for it.
        device_info = {}
    return device, {**device_info, **info}


async def _safe_disconnect(device: Device) -> None:
    """A best-effort disconnect that never raises (used to discard a failed probe)."""
    try:
        await device.disconnect()
    except Exception:  # noqa: BLE001 - best-effort cleanup of the probe connection
        pass
