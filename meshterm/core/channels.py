# SPDX-License-Identifier: Apache-2.0
"""Pure channel logic: secret derivation, MeshCore share URLs (build and parse), and decryption.

A MeshCore channel is a name and a 16-byte shared secret. A *public* channel (its name
starts with ``#``) derives its secret from the name, always in the same way. Thus each
person who gives the channel the same name gets the same key. A *private* channel carries a
random secret, which its users share outside the mesh.

This module holds that logic, with no dependency on the device or on I/O. Thus the create
and join flows, and the ``meshcore://`` share links in the QR codes, can be built,
validated, and unit-tested without hardware. :func:`decrypt_channel_text` uses the same AES
key on an overheard channel text packet that is still encrypted (the packet viewer uses it
for this). Thus the app does not need the decode of the device itself to read a channel for
which it has the key.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import parse_qs, quote, urlsplit

# PyCryptodome is imported in the two functions that need it, never here. The import
# probes the crypto features of the host CPU (``Crypto.Util._cpu_features``). This costs a
# fifth of a second on the Cortex-A7 of the PicoCalc, and each startup pays it, because this
# module is on the boot path through one constant: core.connection imports
# CHANNEL_SLOT_PROBE_CAP, and core.channel_store imports the derivation helpers. Neither of
# them decrypts anything at import. When the import is deferred, the cost moves to the first
# overheard channel packet, where the work is necessary. Each later call only gets the
# module from the sys.modules dict. Both functions bind the names one time for each call,
# not one time for each candidate channel. Thus the reception path pays almost nothing.

#: Matches the ``Name: message`` convention that channel senders use to identify themselves
#: (the protocol has no sender field). The name is 1–20 characters that are not colons, and
#: ``": "`` must follow it. This rule is strict enough that it does not match ``http://…``
#: or ``note:x``.
SENDER_PREFIX = re.compile(r"^([^\s:][^:]{0,19}):[ \t]+(.*)$", re.DOTALL)

#: Matches an ``@[Name]`` mention token, which the reply flow puts into the compose line in
#: advance. A transcript renders each token as a bare ``@Name`` in the hue of that sender,
#: and does not show the brackets. The history face of the CLI also takes out the same
#: names. Thus the token is defined here, with the remainder of the message-body parsing, and
#: not in one of the two renderers. The name is 1–20 characters that are not ``]``.
MENTION = re.compile(r"@\[([^\]]{1,20})\]")


def split_channel_sender(text: str) -> tuple[str | None, str]:
    """Split a channel message into ``(sender_name, body)`` when it has a name prefix.

    Channel messages have no sender field on the wire. Thus senders identify themselves
    with the prefix ``Name: `` at the start of the text. When this function takes out that
    name, a transcript can show it as a coloured header, and a feed can name the sender.
    Also, the message-paths matcher can compare a body from the air with a stored body.

    Args:
        text: The raw text of the channel message.

    Returns:
        ``(name, body)`` when a plausible ``Name: `` prefix is present, else
        ``(None, text)``.
    """
    match = SENDER_PREFIX.match(text)
    if match is None:
        return None, text
    name, body = match.group(1).strip(), match.group(2)
    if not name or name.isdigit() or body.startswith("//"):  # not a name: a URL or a timestamp
        return None, text
    return name, body


#: The MeshCore protocol sets the shared secret of a channel to exactly 16 bytes (128 bits).
CHANNEL_SECRET_BYTES = 16

#: The number of channel slots that MeshTerm assumes when it cannot probe a device. Stock
#: MeshCore companion firmware is built with 8 slots.
#: :meth:`meshterm.core.connection.Device.channel_capacity` finds the real number from the
#: hardware at runtime, so this value is only the fallback.
MAX_CHANNELS = 8

#: The maximum for a slot probe. The wire addresses a channel slot with one byte, so this
#: value is much larger than the slot count of any real firmware. But it still limits a scan
#: of a device that never rejects an index that is out of range (so that the probe cannot
#: loop forever).
CHANNEL_SLOT_PROBE_CAP = 64

#: A sequence of this many empty slots, one after the other, ends a scan for configured
#: channels (refer to :func:`meshterm.core.channel_probe.read_channel_slots`) on firmware
#: that never rejects an index that is out of range. Some builds answer each index with an
#: empty payload instead of an error. Without this limit, the scan walks all
#: :data:`CHANNEL_SLOT_PROBE_CAP` slots at each read. The channel manager puts the channels
#: in sequence from slot 0 up, in the stock :data:`MAX_CHANNELS` slots. Thus no real layout
#: has this many empty slots before its last channel, and a sequence of this length means
#: that the scan has gone past the configured channels. Firmware that does reject an index
#: that is out of range still ends the scan at its true maximum first. (We do not apply this
#: limit to :meth:`meshterm.core.connection.Device.channel_capacity`, on purpose. That
#: method must report the real slot count of a firmware that has more slots and rejects an
#: index. Thus it must get to the rejection, and not guess from a sequence of empty slots.
#: Instead, its cost is limited because MeshTerm caches the result for the session.)
CHANNEL_SLOT_EMPTY_RUN = MAX_CHANNELS

#: The default public channel that each MeshCore device has on slot 0 when it ships.
DEFAULT_PUBLIC_NAME = "public"

#: The built-in public channel of MeshCore ships on slot 0 as ``Public``, with this fixed
#: 16-byte key. This key does not derive from the name. Thus the ``#`` and name-key
#: heuristics cannot recognize the channel, and MeshTerm must match it by its well-known
#: secret instead.
DEFAULT_PUBLIC_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")

#: The secrets of well-known public channels. These channels are public (shared on the whole
#: mesh), whatever their name is, and whether or not their key derives from the name. Today
#: the set holds only the firmware default above. It is a frozenset, so that more
#: authoritative public channels can be added later.
KNOWN_PUBLIC_SECRETS = frozenset({DEFAULT_PUBLIC_SECRET})


def is_public_name(name: str) -> bool:
    """Whether a channel name is a public name, from which the key derives.

    Args:
        name: The channel name.

    Returns:
        ``True`` for a name with a ``#`` prefix (the firmware convention for a key that
        derives from the name), else ``False``.
    """
    return name.startswith("#")


def is_name_derived(name: str, secret: bytes) -> bool:
    """Whether the key of a channel can be calculated from its name.

    ``True`` for a name with a ``#`` prefix, and for each channel whose stored secret is
    already equal to ``derive_secret(name)``. Such a channel does not have to store its key,
    because the firmware calculates the key again from the name. Thus, to re-key or reorder
    this channel, MeshTerm can send no secret at all.

    Args:
        name: The channel name.
        secret: The 16-byte secret stored in the slot.

    Returns:
        ``True`` if the name alone gives the key.
    """
    return is_public_name(name) or bytes(secret) == derive_secret(name)


def is_public_channel(name: str, secret: bytes) -> bool:
    """Whether a channel is public: shared across the mesh, not a private channel off the mesh.

    A channel is public when its key derives from its name (refer to
    :func:`is_name_derived`), or when it is a :data:`KNOWN_PUBLIC_SECRETS` channel. The
    important example is the firmware default ``Public``. Its key is a fixed, well-known
    value, not a value that derives from its name. This second case is why a check of only
    the name or the ``#`` is not sufficient: that check labels this channel as private.

    Args:
        name: The channel name.
        secret: The 16-byte secret stored in the slot.

    Returns:
        ``True`` if the channel is public.
    """
    return is_name_derived(name, secret) or bytes(secret) in KNOWN_PUBLIC_SECRETS


def derive_secret(name: str) -> bytes:
    """Derive the 16-byte secret of a channel from its name, exactly as the firmware does.

    A public channel (with a ``#`` prefix), or any channel created without an explicit key,
    uses ``sha256(name)[:16]``. Thus all persons who type the same name share the same key.

    Args:
        name: The channel name (with the leading ``#``, if the name has one).

    Returns:
        The derived 16-byte secret.
    """
    return sha256(name.encode("utf-8")).digest()[:CHANNEL_SECRET_BYTES]


def random_secret() -> bytes:
    """Generate a new, cryptographically random 16-byte secret for a private channel.

    Returns:
        16 random bytes.
    """
    return secrets.token_bytes(CHANNEL_SECRET_BYTES)


def normalize_secret(text: str) -> bytes:
    """Parse a secret that the user gives (32 hex characters, that is 16 bytes).

    Accepts whitespace around the text, an ``0x`` prefix, and spaces in the text. Thus a key
    that is pasted in any usual form is accepted.

    Args:
        text: The secret as a hex string.

    Returns:
        The 16-byte secret.

    Raises:
        ValueError: If ``text`` is not exactly 16 bytes of hexadecimal.
    """
    cleaned = text.strip().removeprefix("0x").replace(" ", "").replace(":", "")
    data = bytes.fromhex(cleaned)  # raises ValueError: an odd length, or not hex
    if len(data) != CHANNEL_SECRET_BYTES:
        raise ValueError("Channel secret must be exactly 16 bytes (32 hex characters).")
    return data


def full_channel_hash(secret: bytes) -> str:
    """Return the full ``sha256(secret)`` hex digest (64 characters) for a channel.

    Its first byte is the short :func:`channel_hash` that MeshCore reports. MeshTerm shows
    the remainder only for a full fingerprint.

    Args:
        secret: The 16-byte secret of the channel.

    Returns:
        A 64-character lowercase hex string.
    """
    return sha256(bytes(secret)).hexdigest()


def effective_secret(name: str, secret: bytes) -> bytes:
    """Return the key with which a channel encrypts, and resolve a public channel by name.

    The key of a public channel derives from its name. Thus the key material that a device
    has stored in the slot (some firmware and simulators keep zeros) is not its true secret.
    A private channel uses its stored secret without a change.

    Args:
        name: The channel name (a leading ``#`` marks it as public).
        secret: The 16-byte secret stored in the slot.

    Returns:
        The effective 16-byte secret of the channel.
    """
    if is_name_derived(name, secret):
        return derive_secret(name)
    return bytes(secret)


def channel_identity(name: str, secret: bytes) -> str:
    """Return the stable identity of a channel, which does not depend on its slot.

    The identity is the ``sha256`` of the :func:`effective_secret` of the channel. Thus it
    comes from the channel itself (its key material), not from its position in the UX. A
    new order of the channel slots never changes the identity of a channel. Each device that
    shares the channel (a public channel by its name, a private channel by its exchanged
    key) calculates the same value. Because the identity is a hash, the raw secret is not in
    the places where the identity is stored.

    Args:
        name: The channel name.
        secret: The 16-byte secret stored in the slot.

    Returns:
        A 64-character lowercase hex identity.
    """
    return full_channel_hash(effective_secret(name, secret))


def channel_hash(secret: bytes) -> str:
    """Return the channel hash (2 hex characters) that MeshCore shows for a secret.

    This is the first byte of ``sha256(secret)``. It is the same short fingerprint that the
    companion reports for a channel. It helps the user to see quickly that two channels are
    different.

    Args:
        secret: The 16-byte secret of the channel.

    Returns:
        A two-character lowercase hex string.
    """
    return full_channel_hash(secret)[:2]


def share_url(name: str, secret: bytes) -> str:
    """Build the MeshCore ``meshcore://channel/add`` share URL for a channel.

    The MeshCore mobile apps scan exactly this scheme. Thus a QR code rendered from the
    returned string adds the channel directly in the app that scans it.

    Args:
        name: The channel name.
        secret: The channel's 16-byte secret.

    Returns:
        A ``meshcore://channel/add?name=…&secret=…`` URL.
    """
    return f"meshcore://channel/add?name={quote(name, safe='')}&secret={bytes(secret).hex()}"


def parse_share_url(url: str) -> tuple[str, bytes] | None:
    """Parse a ``meshcore://channel/add`` link into ``(name, secret)``.

    Args:
        url: A channel-add share URL (scanned from a QR code, or pasted).

    Returns:
        The channel name and its 16-byte secret, or ``None`` if the string is not a valid
        MeshCore channel-add link.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme != "meshcore":
        return None
    # The URL splits into netloc="channel" and path="/add". Join the two parts, and compare.
    if f"{parts.netloc}{parts.path}".strip("/") != "channel/add":
        return None
    query = parse_qs(parts.query)
    names = query.get("name")
    keys = query.get("secret")
    if not names or not keys:
        return None
    try:
        return names[0], normalize_secret(keys[0])
    except ValueError:
        return None


@dataclass(slots=True)
class DecryptedText:
    """A channel text, recovered from an overheard, encrypted packet.

    Attributes:
        channel_name: The name of the channel whose key decrypted the packet.
        text: The decrypted message body.
        sent_at: The time on the clock of the sender when it composed the message. It is
            present when the embedded timestamp parses to a usable value.
        attempt: The resend counter of the sender for this message (0 = first try).
    """

    channel_name: str
    text: str
    sent_at: datetime | None
    attempt: int


def identify_channel(
    chan_hash: str,
    cipher_mac: str,
    crypted: str,
    channels: Iterable[tuple[str, bytes]],
) -> tuple[str, bytes] | None:
    """Name the channel of an overheard packet, and confirm the key by its MAC.

    The packet identifies its channel only by the one-byte fingerprint of
    :func:`channel_hash`, and a fingerprint is not proof. All the channels on the air go
    into 256 buckets, so several channels can have the same fingerprint (and on a busy
    mesh, they do). The MAC is the proof. Each candidate with a matching fingerprint gets an
    HMAC check against the ciphertext, exactly as the firmware does before it decodes
    anything. The function names a channel only if its key gives the same MAC as the packet.

    This function only names the channel. This is why it is usable on a packet that
    MeshTerm has no reason to decrypt. A channel datagram has the same envelope as a channel
    text, but its body is not text at all, and a feed lane needs only the name of the
    channel. :func:`decrypt_channel_text` does this same confirmation, and then the decode.

    Args:
        chan_hash: The channel-hash fingerprint of the packet (2 hex characters).
        cipher_mac: The MAC of the packet, encoded as hex (2 bytes).
        crypted: The ciphertext of the packet, encoded as hex.
        channels: The candidate channels to try, as ``(name, secret)`` pairs.

    Returns:
        The ``(name, effective key)`` of the channel. ``None`` when the MAC of no known
        channel matches: a channel for which we do not have the key, or only a collision
        of fingerprints.
    """
    from Crypto.Hash import HMAC  # deferred: refer to the module header
    from Crypto.Hash import SHA256 as _SHA256

    try:
        mac = bytes.fromhex(cipher_mac)
        msg = bytes.fromhex(crypted)
    except ValueError:
        return None
    if not msg:
        return None
    for name, secret in channels:
        key = effective_secret(name, secret)
        if channel_hash(key) != chan_hash:
            continue
        mac_check = HMAC.new(key, digestmod=_SHA256)
        mac_check.update(msg)
        if mac_check.digest()[:2] == mac:
            return name, key
    return None  # an unknown channel, or the same fingerprint with a key that does not match


def decrypt_channel_text(
    chan_hash: str,
    cipher_mac: str,
    crypted: str,
    channels: Iterable[tuple[str, bytes]],
) -> DecryptedText | None:
    """Recover the plaintext of a GRP_TXT packet with a set of known channels.

    Does the same decode that the firmware does for an overheard channel text packet.
    First, the MAC confirms the channel (:func:`identify_channel`), because a fingerprint
    alone can collide. Only the key that the MAC confirmed is trusted to decrypt anything.
    A packet from a channel that is not in ``channels`` gives ``None``. A packet whose
    fingerprint matches but whose MAC does not match (a true collision) also gives ``None``.
    This is the same result as firmware that does not know the channel.

    Args:
        chan_hash: The channel-hash fingerprint of the packet (2 hex characters).
        cipher_mac: The MAC of the packet, encoded as hex (2 bytes).
        crypted: The ciphertext of the packet, encoded as hex (a whole number of AES
            blocks).
        channels: The candidate channels to try, as ``(name, secret)`` pairs.

    Returns:
        The decrypted text and its channel. ``None`` if the MAC of no known channel
        matches, or if the ciphertext is malformed.
    """
    from Crypto.Cipher import AES  # deferred: refer to the module header

    try:
        msg = bytes.fromhex(crypted)
    except ValueError:
        return None
    if not msg or len(msg) % AES.block_size:
        return None  # not a whole number of blocks: not a GRP_TXT body to decrypt
    identified = identify_channel(chan_hash, cipher_mac, crypted, channels)
    if identified is None:
        return None
    name, key = identified
    plain = AES.new(key, AES.MODE_ECB).decrypt(msg)
    timestamp = int.from_bytes(plain[0:4], "little")
    attempt = plain[4] & 0x03
    text = plain[5:].strip(b"\x00").decode("utf-8", "ignore")
    sent_at = None
    if timestamp:
        try:
            sent_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            sent_at = None
    return DecryptedText(channel_name=name, text=text, sent_at=sent_at, attempt=attempt)
