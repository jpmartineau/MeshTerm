# SPDX-License-Identifier: Apache-2.0
"""Region scopes: the maths that connects a flood to the region into which it was sent.

A MeshCore repeater can get a list of the *regions* that it carries (``region put yul``,
``region allowf yul``). When a flood is *scoped* to a region, only the repeaters that
carry that region relay it. The scope is in the packet as a **transport code**. A
``TRANSPORT_FLOOD`` packet (route type 0, which the library calls ``TC_FLOOD``) has two
little-endian ``uint16`` codes between its header and its path. The first code is

    HMAC-SHA256(region key, payload_type_byte || payload)[0:2]

with ``0x0000`` and ``0xFFFF`` reserved (they become ``0x0001`` and ``0xFFFE``). The
second code is reserved, and at this time it is always zero. The key of a region is the
first 16 bytes of ``SHA-256("#" + name)``, with the name as its UTF-8 bytes. The firmware
stores names bare, and puts the ``#`` at the start when it derives the key
(``RegionMap::getTransportKeysFor``, ``TransportKeyStore``). Each companion app does the
same.

Two results of this design have an effect on all the code that uses this module:

* **A code names no region.** It is a keyed hash of the payload of the packet. Thus the
  only way to find the region of a scoped packet is to try the known region names, and to
  find the name whose key makes the same code (:func:`resolve`). If a packet is scoped to
  a region that nobody here has named, it shows as *scoped, region unknown*. This result
  is honest, and it stays until somebody here names that region.
* **The code does not change on the way.** The hash includes neither the header nor the
  path. Thus the copy of each relay carries the code of the sender without a change. One
  message has one scope, also when it arrived by many different relays.

A repeater never filters a direct packet by region. Thus a scope is important only for a
flood. The codes of a ``TC_DIRECT`` packet are the ``{0, 0}`` "to nowhere" pair that a
contact share uses, not a region.

This module sets the words, because the UI uses them everywhere. A **region** is a named
area that a repeater carries. The **scope** of a flood is the region to which the flood
is limited. A plain flood is **unscoped**. The region list of a repeater shows this as
the wildcard ``*``.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass

#: The wildcard: "every region", that is, unscoped. Repeaters list it in this form, and
#: the explicit unscoped override of the send scope takes it in this form.
WILDCARD = "*"

#: The longest region name, in UTF-8 bytes. The name slot of the firmware is 31 bytes with
#: the terminating NUL (``RegionEntry.name[31]``, and the 31-byte field for the default
#: scope in the companion protocol). Also, ``CMD_SET_DEFAULT_FLOOD_SCOPE`` refuses a
#: length outside 1–30.
MAX_NAME_BYTES = 30

#: The width of the transport key of a region, in bytes.
KEY_BYTES = 16

#: The route types that carry transport codes, with the names that the meshcore library
#: uses (``ROUTE_TYPENAMES``): a scoped flood and its direct equivalent.
SCOPED_ROUTES = frozenset({"TC_FLOOD", "TC_DIRECT"})

#: The only route types that a repeater filters by region: floods, scoped or not. A
#: repeater never filters a direct packet by region, so a direct packet has no scope to
#: show.
FLOOD_ROUTES = frozenset({"FLOOD", "TC_FLOOD"})


class RegionNameError(ValueError):
    """A region name that the firmware refuses, with a reason that the user can act on."""


def normalize(name: str) -> str:
    """A region name as the firmware stores and shows it: trimmed, with no ``#`` at the start.

    Apps and older firmware wrote names as ``#yul``. From firmware 1.12, the names are
    stored bare, and the ``#`` is only for the key derivation. The case is kept, because
    the hash is over the exact bytes. Thus ``YUL`` and ``yul`` are two different regions.

    Args:
        name: The name as the user typed it, as a repeater listed it, or as it was read
            from a device.

    Returns:
        The bare name (it can be empty).
    """
    return name.strip().removeprefix("#").strip()


def validate(name: str) -> str:
    """Normalize a region name, and refuse a name that the firmware refuses.

    Args:
        name: The name as typed.

    Returns:
        The bare, valid name.

    Raises:
        RegionNameError: If the name is empty, too long, private, or the wildcard.
    """
    bare = normalize(name)
    if not bare:
        raise RegionNameError("a region needs a name")
    if bare == WILDCARD:
        raise RegionNameError("* is every region — leave the scope empty for unscoped")
    if bare.startswith("$"):
        # A private region gets its key from a hardware keystore that the firmware does
        # not have yet (``RegionMap::loadKeysFor`` is a TODO). Thus no repeater can match
        # a private region.
        raise RegionNameError("$ regions are private and no firmware can match one yet")
    if len(bare.encode("utf-8")) > MAX_NAME_BYTES:
        raise RegionNameError(f"a region name is at most {MAX_NAME_BYTES} bytes")
    if any(ch.isspace() or ch == "," for ch in bare):
        # A repeater lists its regions with commas between them, and it splits its CLI
        # input at whitespace. Thus a repeater can store a name that has a comma or a
        # space, but it can never list that name or address it again.
        raise RegionNameError("a region name has no spaces or commas")
    return bare


def region_key(name: str) -> bytes:
    """The 16-byte transport key of a region, as the firmware derives it.

    Args:
        name: The region name, with or without the ``#`` at its start.

    Returns:
        ``SHA-256("#" + bare name)[:16]``, with the name as UTF-8.
    """
    return hashlib.sha256(("#" + normalize(name)).encode("utf-8")).digest()[:KEY_BYTES]


def transport_code(key: bytes, body: bytes) -> int:
    """The transport code that a packet carries when it is scoped under ``key``.

    Args:
        key: The 16-byte transport key of the region (:func:`region_key`).
        body: The HMAC input: the payload type byte, then the payload
            (:func:`scope_body`).

    Returns:
        The 16-bit code. If the code is one of the two reserved values, it moves one step
        away from that value.
    """
    code = int.from_bytes(hmac.new(key, body, hashlib.sha256).digest()[:2], "little")
    if code == 0x0000:
        return 0x0001
    if code == 0xFFFF:
        return 0xFFFE
    return code


def scope_body(payload_type: int, payload: bytes) -> bytes:
    """The bytes from which a transport code is calculated: the payload type, then the payload.

    Args:
        payload_type: The 4-bit payload type of the header (``GRP_TXT`` is 5, ``ADVERT``
            4, …).
        payload: The payload of the packet: all the bytes after the path.

    Returns:
        ``bytes([payload_type]) + payload``.
    """
    return bytes([payload_type & 0x0F]) + bytes(payload)


def parse_codes(hex4: str | None) -> tuple[int, int] | None:
    """Split the 4-byte transport-codes field of a packet into its two little-endian codes.

    Args:
        hex4: The field as the meshcore library gives it (``transport_code``, 8 hex
            digits), or ``None`` when the packet had no such field.

    Returns:
        ``(code0, code1)``, or ``None`` for a field that is absent or malformed.
    """
    if not hex4:
        return None
    try:
        raw = bytes.fromhex(str(hex4))
    except ValueError:
        return None
    if len(raw) != 4:
        return None
    return int.from_bytes(raw[0:2], "little"), int.from_bytes(raw[2:4], "little")


@dataclass(frozen=True, slots=True)
class Scope:
    """What a received packet tells about the region into which it was flooded.

    Attributes:
        state: ``"unscoped"``: a plain flood that each repeater can relay. ``"scoped"``:
            a scoped flood whose region is :attr:`region`. ``"unknown"``: a scoped flood
            whose code matches no region that MeshTerm knows.
        region: The bare region name for a resolved scope, else ``None``.
        code: The first transport code of the packet (4 lowercase hex digits), for a
            scoped packet.
    """

    state: str
    region: str | None = None
    code: str | None = None

    @property
    def scoped(self) -> bool:
        """Whether the flood was scoped, resolved or not."""
        return self.state != "unscoped"


#: The only unscoped answer. All callers share it, because a Scope is immutable.
UNSCOPED = Scope("unscoped")


def resolve(names: Iterable[str], body: bytes, code: int) -> str | None:
    """The first known region whose key makes ``code`` from ``body``.

    Two different regions can have the same 16-bit code for one payload (one chance in
    65 534 for each pair). The firmware has the same ambiguity: a repeater that carries
    both regions relays the packet under the region that it lists first. Here also, the
    answer is the first match in the order of ``names``. The callers put the most probable
    names first.

    Args:
        names: The candidate region names (bare or with a ``#`` prefix).
        body: The HMAC input (:func:`scope_body`).
        code: The first transport code of the packet.

    Returns:
        The bare name that matches, or ``None``.
    """
    for name in names:
        bare = normalize(name)
        if bare and bare != WILDCARD and transport_code(region_key(bare), body) == code:
            return bare
    return None


def frame_scope(raw: dict, names: Iterable[str]) -> Scope | None:
    """The scope of the raw payload of a received packet, or ``None`` if it has no scope.

    The function reads the route type and the transport codes that the meshcore library
    parses from an RX-log packet (or that the repository restores for a replayed packet).
    It also reads the HMAC input. The HMAC input is made live from
    ``payload_type``/``pkt_payload``, or restored as ``scope_body``.

    Args:
        raw: The raw payload mapping of the packet.
        names: The region names against which to resolve a scoped packet.

    Returns:
        :data:`UNSCOPED` for a plain flood, a scoped :class:`Scope` for a transport flood,
        and ``None`` for a direct packet or a packet whose route type was never kept. A
        repeater never filters these packets by region, so they have no scope to show.
    """
    route = str(raw.get("route_typename") or "").upper()
    if route == "FLOOD":
        return UNSCOPED
    if route != "TC_FLOOD":
        return None
    codes = parse_codes(raw.get("transport_code"))
    if codes is None:
        return Scope("unknown")
    code0 = codes[0]
    body = raw_scope_body(raw)
    region = resolve(names, body, code0) if body is not None else None
    return Scope("scoped" if region else "unknown", region, f"{code0:04x}")


def raw_scope_body(raw: dict) -> bytes | None:
    """The HMAC input for the raw payload of a packet, live or restored from the history.

    Args:
        raw: The raw payload mapping of the packet.

    Returns:
        The body bytes, or ``None`` when there are no live fields and no stored body.
    """
    stored = raw.get("scope_body")
    if stored:
        try:
            return bytes.fromhex(str(stored))
        except ValueError:
            return None
    payload_type = raw.get("payload_type")
    payload = raw.get("pkt_payload")
    if payload_type is None or payload is None or int(payload_type) < 0:
        return None
    if isinstance(payload, str):
        try:
            payload = bytes.fromhex(payload)
        except ValueError:
            return None
    return scope_body(int(payload_type), bytes(payload))


def parse_region_list(text: str | None) -> list[str]:
    """The regions that a repeater names in its reply to the ``REGIONS`` anonymous request.

    The reply is a list of the regions for which the repeater relays floods, with commas
    between them. If the repeater also relays unscoped floods, ``*`` comes first (firmware
    ``RegionMap::exportNamesTo``).

    Args:
        text: The text of the reply (the meshcore library removes the clock that comes
            before it).

    Returns:
        The bare names in the given order. The wildcard stays as ``*``, and the function
        removes blank names.
    """
    if not text:
        return []
    names: list[str] = []
    for part in str(text).replace("\x00", "").split(","):
        bare = normalize(part)
        if bare and bare not in names:
            names.append(bare)
    return names
