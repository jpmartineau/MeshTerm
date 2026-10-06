# SPDX-License-Identifier: Apache-2.0
"""The addressing of an overheard packet: the part of the packet body that no other code parses.

The RX packet log of the companion gives each overheard packet to the meshcore library.
The library separates the header, which it always understands: the route type, the
payload class, and the relay path. It keeps the remainder as an undecoded ``pkt_payload``
blob. Then the library decodes that blob for only two classes: an advert (the identity
and the location in it) and a channel text packet (its channel fingerprint, MAC, and
ciphertext). Each other class arrives as bytes: the direct messages, requests, responses,
path returns, acks, and traces. These are half of the packets that a busy mesh transmits.
For this reason, a flood without an origin once showed nothing about itself except its
class.

But these bytes are not opaque data. MeshCore puts the addressing of a packet in the first
few bytes, and the layout is fixed for each payload class. The protocol publishes these
layouts in its ``docs/payloads.md`` and ``docs/packet_format.md``:

===========================  ==========================================================
class                        body layout
===========================  ==========================================================
``REQ``/``RESPONSE``/        ``[dest hash:1][src hash:1][MAC:2][ciphertext]``
``TEXT_MSG``/``PATH``
``ANON_REQ``                 ``[dest hash:1][sender key:32][MAC:2][ciphertext]``
``ACK``                      ``[checksum:4]``
``TRACE``                    ``[tag:4][auth code:4][flags:1][hops…]``
``GRP_TXT``/``GRP_DATA``     ``[channel hash:1][MAC:2][ciphertext]``
===========================  ==========================================================

One class also breaks the rule that the header follows. In each other packet, the ``path``
field is the list of the relay hashes that the packet went through. In a ``TRACE``, it is a
list of **signed SNR bytes**, one for each hop that the packet went through. Refer to
:func:`trace_link_snrs`, which reads this field. That function exists because MeshTerm
once read these bytes as hashes, and this put false adjacency into the topology graph.

An endpoint is identified by a hash: the first byte of the public key of the node. This is
the same one-byte identity that the hops of the relay path use. Thus endpoints resolve
through the usual node resolver of the app, and they get the same names, hues, and
collisions as each hop. An anonymous request is the exception. It has no shared secret yet
by which the receiver can recognize it, so it carries the full public key of its sender.
(The protocol documents call it the "sender's Ed25519 public key". The comment in the
``Packet.h`` header still calls it "ephemeral". But the login payloads that it carries go
to a node that must know who logged in, and the field table of the documents is the later
statement.) This module recovers only the addressing: for whom a packet is, from whom it
says it is, the channel that it is part of, and the token that it carries. This module does
not decrypt the ciphertext. (A channel for which we have the key is decrypted in
:mod:`~meshterm.core.channels`, by a key that the MAC confirms. No other data on the mesh
is ours to read.)

MeshTerm does two checks before it trusts one byte, because a wrong slice does not cause an
error: it gives a hash that looks correct. First, the payload version of the packet
must be the version that these layouts describe. This is v1, the only version in use, and
the only version with one-byte hashes and a two-byte MAC. (The header keeps two bits for a
v2 that makes both of them wider.) Second, the body must be long enough for the layout
that its class promises. If one of the two checks fails, the result is empty.

The recovered fields are merged directly into the raw payload of the packet (refer to
:func:`~meshterm.core.connection.packet_observation_from_event`), each under its own key.
Thus each part of the app that reads the payload (the lane of the live feed, the card of
the packet viewer, the stored columns of the repository) reads these fields the same way
that it reads the two classes that the library decoded itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The payload classes that MeshCore addresses with a pair of one-byte key hashes: first the
#: recipient, then the sender, then the MAC and the ciphertext. A direct message, a request
#: to a repeater, its response, and a returned path all use this envelope.
ADDRESSED_CLASSES = frozenset({"REQ", "RESPONSE", "TEXT_MSG", "PATH"})

#: The payload classes that carry the ``[channel hash][MAC][ciphertext]`` channel envelope:
#: the text messages of a channel and its datagrams. This module decodes only ``GRP_DATA``,
#: because the library already decodes ``GRP_TXT`` itself, into the same field names. But
#: after this point, the same code stores, decrypts, and names the two classes.
CHANNEL_CLASSES = frozenset({"GRP_TXT", "GRP_DATA"})

#: The number of bytes of a public key by which a packet identifies an endpoint. It is one:
#: the same one-byte identity that a hop of a relay path carries. This is why an endpoint
#: resolves through the usual node resolver (and has the same collisions as a hop).
#: :data:`_PAYLOAD_V1` sets this value.
ENDPOINT_HASH_BYTES = 1

#: The payload version that these layouts describe: 1-byte endpoint hashes and a 2-byte
#: MAC. The two version bits of the header keep a v2 that makes both of them wider. Thus a
#: packet that announces a different version stays undecoded. MeshTerm does not slice it
#: with the offsets of v1.
_PAYLOAD_V1 = 0

#: The number of bytes of the MAC that follows the addressing.
_MAC = 2
#: The number of bytes of a full public key. An anonymous request carries the full key of
#: its sender.
_KEY = 32
#: The number of bytes of the token of a packet: the checksum of an ack, or the tag of a
#: trace.
_TOKEN = 4


def frame_addressing(payload: Mapping[str, Any]) -> dict[str, str]:
    """Recover the addressing of a raw packet from the RX log, from its undecoded body.

    Reads the first bytes of ``pkt_payload`` in the layout of the payload class of the
    packet (refer to the table in the module docstring). Returns them as hex, under the
    raw-payload keys that the remainder of the app reads:

    * ``dest_hash``: the one-byte key hash of the recipient (each addressed class).
    * ``src_hash``: the one-byte key hash of the sender (the classes with two hashes).
    * ``cipher_mac``: the two-byte MAC that follows the hashes. It is a tag over the
      encrypted message. Thus it is a fingerprint that identifies which message a packet
      carries, but it does not let MeshTerm read that message.
    * ``src_key``: the full public key of the sender. An anonymous request carries it
      instead of a hash, because the request has no shared secret yet by which the
      receiver can recognize it.
    * ``ack_crc``: the four-byte checksum in an ack of the message that the ack
      acknowledges, as hex in wire order. The companion reports its own delivery acks in
      the same form, so MeshTerm can compare the two.
    * ``trace_tag``: the tag of a trace, the token that matches the trace to its reply. It
      is read as the little-endian ``uint32`` that the firmware writes, and shown as eight
      hex digits. Thus it is the same number as the ``tag`` of a trace reply, not that
      number with its bytes in reverse order.
    * ``chan_hash``/``cipher_mac``/``crypted``: the envelope of a channel datagram. These
      are the same three fields into which the library divides a channel text packet.

    Hashes and keys are the bytes in their order on the wire, encoded as hex. Thus a hash
    has the same form as the path hops next to it.

    Args:
        payload: The raw payload of the packet, as the RX-log event carried it. It must
            have ``payload_typename`` and the undecoded ``pkt_payload`` bytes. If
            ``payload_ver`` is present, the function obeys it.

    Returns:
        The recovered fields. The mapping is empty when the class carries no addressing,
        when the body is missing, when the packet announces a payload version that these
        layouts do not describe, or when the body is too short for the layout that its
        class promises.
    """
    typename = payload.get("payload_typename")
    body = payload.get("pkt_payload")
    if not typename or not isinstance(body, (bytes, bytearray)):
        return {}
    version = payload.get("payload_ver")
    if version is not None and version != _PAYLOAD_V1:
        return {}  # a wider hash and MAC: the v1 offsets get the wrong bytes
    body = bytes(body)

    hash_w = ENDPOINT_HASH_BYTES
    if typename in ADDRESSED_CLASSES:
        if len(body) < hash_w * 2 + _MAC:
            return {}
        return {
            "dest_hash": body[:hash_w].hex(),
            "src_hash": body[hash_w : hash_w * 2].hex(),
            # The MAC of an addressed packet is a tag over the plaintext of this message.
            # Thus two packets with the same MAC (between the same pair) are copies of the
            # same message. MeshTerm can compare this fingerprint without a key. With it,
            # the message-paths screen can group the retransmissions of a direct message
            # and separate them from those of the next message. For a channel packet that
            # we can decrypt, a match of the content does this (refer to
            # :mod:`~meshterm.services.message_paths`).
            "cipher_mac": body[hash_w * 2 : hash_w * 2 + _MAC].hex(),
        }
    if typename == "ANON_REQ":
        if len(body) < hash_w + _KEY + _MAC:
            return {}
        return {
            "dest_hash": body[:hash_w].hex(),
            "src_key": body[hash_w : hash_w + _KEY].hex(),
        }
    if typename == "ACK":
        if len(body) < _TOKEN:
            return {}
        return {"ack_crc": body[:_TOKEN].hex()}
    if typename == "TRACE":
        # The tag, the auth code, and the flags. The shortest trace has no hops yet.
        if len(body) < _TOKEN * 2 + 1:
            return {}
        # The firmware copies the tag directly from a uint32 (memcpy), and the library
        # reads the tag of a trace reply the same way: little-endian. Thus the two agree
        # on the number.
        return {"trace_tag": f"{int.from_bytes(body[:_TOKEN], 'little'):08x}"}
    if typename == "GRP_DATA":  # like GRP_TXT, but the library does not decode it
        if len(body) < hash_w + _MAC:
            return {}
        return {
            "chan_hash": body[:hash_w].hex(),
            "cipher_mac": body[hash_w : hash_w + _MAC].hex(),
            "crypted": body[hash_w + _MAC :].hex(),
        }
    return {}


def trace_link_snrs(payload: Mapping[str, Any]) -> list[float] | None:
    """Recover the link reading of each hop that a ``TRACE`` packet collected, from its path.

    A trace is the only class in which the ``path`` field of the header does not hold relay
    hashes. The firmware adds **one signed SNR byte for each hop that the packet goes
    through**. Each byte is the SNR at which a node that relayed the packet heard the node
    before it. This is how a trace reply can report the link quality of a full route
    without a second field. It is also the subject of the warning
    ``# Beware of traces where pathes are mixed`` in the meshcore parser. If MeshTerm reads
    these bytes as hashes, the result is not only useless but false. The bytes resolve to
    the nodes that have the same first digits by chance. Then they go into the topology
    graph as adjacency that nobody observed.

    This difference is measured, not theoretical. In a captured population of 1,604 traces
    (2,221 path entries), each entry, read as a signed byte divided by four, is in the LoRa
    SNR band, from −10.5 to +15.0 dB. But relay hashes are uniform over the byte, so
    approximately five in six relay hashes are outside that band.

    Args:
        payload: The raw payload of the packet, as the RX-log event carried it. It must
            have ``payload_typename``, ``path_len``, and the hex ``path``.

    Returns:
        One SNR in dB for each hop that the packet went through, in the order of the hops.
        ``None`` when the packet is not a trace, or when its path field cannot be read at
        the announced length. For a trace that no node relayed yet, the result is an empty
        list, and this result is correct.
    """
    if payload.get("payload_typename") != "TRACE":
        return None
    try:
        hop_count = int(payload.get("path_len") or 0)
    except (TypeError, ValueError):
        return None
    if hop_count < 0:
        return None
    raw = str(payload.get("path") or "").lower().removeprefix("0x")
    try:
        readings = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(readings) < hop_count:
        return None  # shorter than announced: the missing end can only be invented
    return [_snr_db(b) for b in readings[:hop_count]]


def _snr_db(byte: int) -> float:
    """Decode one SNR byte from the wire: a signed value in quarter decibels."""
    return (byte - 256 if byte >= 128 else byte) / 4.0
