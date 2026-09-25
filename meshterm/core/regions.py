# SPDX-License-Identifier: Apache-2.0
"""Region scopes: the maths that ties a flood to the region it was sent into.

MeshCore repeaters can be told which *regions* they carry (``region put yul``,
``region allowf yul``), and a flood sent *scoped* to a region is relayed only by the
repeaters that carry it. The scope travels in the packet as a **transport code**: a
``TRANSPORT_FLOOD`` frame (route type 0 — the library's ``TC_FLOOD``) carries two
little-endian ``uint16`` codes between its header and its path, and the first is

    HMAC-SHA256(region key, payload_type_byte || payload)[0:2]

with ``0x0000`` and ``0xFFFF`` reserved (they become ``0x0001`` and ``0xFFFE``). The
second code is reserved and always zero today. A region's key is the first 16 bytes of
``SHA-256("#" + name)``, the name taken as its UTF-8 bytes — the firmware stores names
bare and puts the ``#`` in front when it derives the key (``RegionMap::getTransportKeysFor``,
``TransportKeyStore``), and so does every companion app.

Two consequences shape everything built on this module:

* **A code names no region.** It is a keyed hash of the packet's own payload, so the one
  way to say which region a scoped packet belongs to is to try the region names already
  known and see whose key reproduces it (:func:`resolve`). A packet scoped to a region
  nobody here has named reads as *scoped, region unknown* — honestly, and for good.
* **The code survives the trip.** Neither the header nor the path is in the hash, so every
  relay's copy carries the sender's code unchanged: one message has one scope however many
  ways it arrived.

Direct-routed packets are never region-filtered by a repeater, so a scope matters to a
flood alone; a ``TC_DIRECT`` frame's codes are the ``{0, 0}`` "to nowhere" pair a contact
share uses, not a region.

The vocabulary, fixed here because the UI speaks it everywhere: a **region** is a named
area a repeater carries; a flood's **scope** is the region it is limited to; a plain flood
is **unscoped**, which a repeater's region list spells as the wildcard ``*``.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass

#: The wildcard — "every region", i.e. unscoped — as repeaters list it and as the send
#: scope's explicit-unscoped override takes it.
WILDCARD = "*"

#: The longest region name, in UTF-8 bytes. The firmware's name slot is 31 bytes with the
#: terminating NUL (``RegionEntry.name[31]``, the default-scope frame's 31-byte field), and
#: ``CMD_SET_DEFAULT_FLOOD_SCOPE`` refuses anything outside 1–30.
MAX_NAME_BYTES = 30

#: Width of a region's transport key, in bytes.
KEY_BYTES = 16

#: The route types that carry transport codes, as the meshcore library names them
#: (``ROUTE_TYPENAMES``): a scoped flood and its direct sibling.
SCOPED_ROUTES = frozenset({"TC_FLOOD", "TC_DIRECT"})

#: The route types a repeater filters by region at all — floods, scoped or not. A direct
#: packet is never region-filtered, so it has no scope to show.
FLOOD_ROUTES = frozenset({"FLOOD", "TC_FLOOD"})


class RegionNameError(ValueError):
    """A region name the firmware would refuse, with the reason in words a reader can act on."""


def normalize(name: str) -> str:
    """A region name as the firmware stores and shows it: trimmed, with no leading ``#``.

    Apps and older firmware wrote names as ``#yul``; since firmware 1.12 they are stored
    bare and the ``#`` belongs only to key derivation. Case is kept — the hash is over the
    exact bytes, so ``YUL`` and ``yul`` are two different regions.

    Args:
        name: The name as typed, listed by a repeater, or read back from a device.

    Returns:
        The bare name (possibly empty).
    """
    return name.strip().removeprefix("#").strip()


def validate(name: str) -> str:
    """Normalize a region name and refuse one the firmware would refuse.

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
        # Private regions derive their keys from a hardware keystore the firmware has not
        # built yet (``RegionMap::loadKeysFor`` is a TODO), so no repeater can match one.
        raise RegionNameError("$ regions are private and no firmware can match one yet")
    if len(bare.encode("utf-8")) > MAX_NAME_BYTES:
        raise RegionNameError(f"a region name is at most {MAX_NAME_BYTES} bytes")
    if any(ch.isspace() or ch == "," for ch in bare):
        # A repeater lists its regions comma-separated and parses its CLI on whitespace, so
        # a name holding either could be stored but never listed or addressed again.
        raise RegionNameError("a region name has no spaces or commas")
    return bare


def region_key(name: str) -> bytes:
    """The 16-byte transport key of a region, as the firmware derives it.

    Args:
        name: The region name, with or without its leading ``#``.

    Returns:
        ``SHA-256("#" + bare name)[:16]``, the name taken as UTF-8.
    """
    return hashlib.sha256(("#" + normalize(name)).encode("utf-8")).digest()[:KEY_BYTES]


def transport_code(key: bytes, body: bytes) -> int:
    """The transport code a packet carries when scoped under ``key``.

    Args:
        key: The region's 16-byte transport key (:func:`region_key`).
        body: The HMAC input: the payload type byte followed by the payload
            (:func:`scope_body`).

    Returns:
        The 16-bit code, with the two reserved values stepped off.
    """
    code = int.from_bytes(hmac.new(key, body, hashlib.sha256).digest()[:2], "little")
    if code == 0x0000:
        return 0x0001
    if code == 0xFFFF:
        return 0xFFFE
    return code


def scope_body(payload_type: int, payload: bytes) -> bytes:
    """The bytes a transport code is computed over: the payload type, then the payload.

    Args:
        payload_type: The header's 4-bit payload type (``GRP_TXT`` is 5, ``ADVERT`` 4, …).
        payload: The packet's payload — everything after the path.

    Returns:
        ``bytes([payload_type]) + payload``.
    """
    return bytes([payload_type & 0x0F]) + bytes(payload)


def parse_codes(hex4: str | None) -> tuple[int, int] | None:
    """Split a frame's 4-byte transport-codes field into its two little-endian codes.

    Args:
        hex4: The field as the meshcore library reports it (``transport_code``, 8 hex
            digits), or ``None`` where the frame carried none.

    Returns:
        ``(code0, code1)``, or ``None`` for an absent or malformed field.
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
    """What a received frame says about the region it was flooded into.

    Attributes:
        state: ``"unscoped"`` — a plain flood, every repeater may relay it;
            ``"scoped"`` — a scoped flood whose region is :attr:`region`; ``"unknown"`` — a
            scoped flood whose code matches no region known here.
        region: The bare region name for a resolved scope, else ``None``.
        code: The frame's first transport code (lowercase 4-hex), for a scoped frame.
    """

    state: str
    region: str | None = None
    code: str | None = None

    @property
    def scoped(self) -> bool:
        """Whether the flood was scoped at all (resolved or not)."""
        return self.state != "unscoped"


#: The one unscoped answer, shared (a Scope is immutable).
UNSCOPED = Scope("unscoped")


def resolve(names: Iterable[str], body: bytes, code: int) -> str | None:
    """The first known region whose key reproduces ``code`` over ``body``.

    Two distinct regions can collide on a 16-bit code for one payload (one chance in
    65 534 per pair), and the firmware has the same ambiguity — a repeater that carries
    both relays the packet under whichever it lists first. The first match in ``names``
    order is the answer here too; callers order names by how likely they are.

    Args:
        names: Candidate region names (bare or ``#``-prefixed).
        body: The HMAC input (:func:`scope_body`).
        code: The frame's first transport code.

    Returns:
        The matching bare name, or ``None``.
    """
    for name in names:
        bare = normalize(name)
        if bare and bare != WILDCARD and transport_code(region_key(bare), body) == code:
            return bare
    return None


def frame_scope(raw: dict, names: Iterable[str]) -> Scope | None:
    """The scope of a received frame's raw payload, or ``None`` where it has none.

    Reads the route type and transport codes the meshcore library parses out of an
    RX-log frame (or that the repository restores for a replayed one), and the HMAC input
    — built live from ``payload_type``/``pkt_payload``, or restored as ``scope_body``.

    Args:
        raw: The frame's raw payload mapping.
        names: Region names to resolve a scoped frame against.

    Returns:
        :data:`UNSCOPED` for a plain flood, a scoped :class:`Scope` for a transport flood,
        and ``None`` for a direct-routed frame or one whose route type was never kept —
        a repeater never region-filters those, so there is no scope to state.
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
    """The HMAC input for a frame's raw payload, live or restored from history.

    Args:
        raw: The frame's raw payload mapping.

    Returns:
        The body bytes, or ``None`` where neither the live fields nor a stored body exist.
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
    """The regions a repeater's ``REGIONS`` anonymous-request reply names.

    The reply is a comma-separated list of the regions the repeater relays floods for,
    ``*`` first when it relays unscoped floods too (firmware ``RegionMap::exportNamesTo``).

    Args:
        text: The reply's text (the meshcore library strips the clock ahead of it).

    Returns:
        The bare names in the order given, the wildcard kept as ``*``, blanks dropped.
    """
    if not text:
        return []
    names: list[str] = []
    for part in str(text).replace("\x00", "").split(","):
        bare = normalize(part)
        if bare and bare not in names:
            names.append(bare)
    return names
