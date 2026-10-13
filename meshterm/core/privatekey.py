# SPDX-License-Identifier: Apache-2.0
"""The private key of our node: the forms that MeshTerm accepts, and the one form that it sends.

A MeshCore companion exports and imports its private key in one form only: 64 bytes, the
expanded Ed25519 key of the orlp library that the firmware uses (a 32-byte scalar, then a
32-byte nonce). In hex, this key has 128 digits.

A key can also exist in a shorter form: the 32-byte Ed25519 seed from which the 64 bytes
come. The node that MeshTerm runs on an SPI radio keeps such a seed in its ``identity.key``
when it makes its own key. A seed is the same identity, but a firmware companion does not
accept it. Thus MeshTerm expands a seed before it sends it (:func:`expand_seed`), in the
same way that the firmware makes its own key. The user can give either form, and the
device always gets the 64 bytes.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

__all__ = [
    "KEY_BYTES",
    "SEED_BYTES",
    "expand_seed",
    "normalize",
    "parse_private_key",
    "read_private_key_file",
]

#: The length of a private key in the form that a companion exports and imports.
KEY_BYTES = 64

#: The length of an Ed25519 seed, the short form from which a key comes.
SEED_BYTES = 32

#: The largest file that :func:`read_private_key_file` reads. A key in hex, with a line
#: break, is 129 bytes. Thus a larger file is not a key, and the function does not read a
#: large file that the user selected by mistake.
_MAX_FILE_BYTES = 4096

_HEX = re.compile(r"[0-9a-fA-F]*")


def expand_seed(seed: bytes) -> bytes:
    """The 64-byte private key that a 32-byte Ed25519 seed makes.

    This is the key generation of the orlp Ed25519 library, which the MeshCore firmware
    uses: the SHA-512 digest of the seed, with its first half clamped to make the scalar.
    The result has the same public key as the seed, and a firmware companion accepts it.

    Raises:
        ValueError: If ``seed`` does not have 32 bytes.
    """
    if len(seed) != SEED_BYTES:
        raise ValueError(f"A seed has {SEED_BYTES} bytes, not {len(seed)}.")
    digest = hashlib.sha512(seed).digest()
    scalar = bytearray(digest[:32])
    scalar[0] &= 0xF8
    scalar[31] &= 0x3F
    scalar[31] |= 0x40
    return bytes(scalar) + digest[32:]


def normalize(raw: bytes) -> bytes:
    """``raw`` as a 64-byte key: a key stays as it is, and a seed is expanded.

    Raises:
        ValueError: If ``raw`` is neither a key nor a seed. The message is for the user.
    """
    if len(raw) == KEY_BYTES:
        return bytes(raw)
    if len(raw) == SEED_BYTES:
        return expand_seed(raw)
    raise ValueError(
        f"A private key has {KEY_BYTES} bytes ({KEY_BYTES * 2} hex digits), or "
        f"{SEED_BYTES} for a seed. This one has {len(raw)}."
    )


def parse_private_key(text: str) -> bytes:
    """The 64-byte key in ``text``, which the user typed or pasted as hex.

    The function ignores spaces and line breaks, because a key that the user copies from a
    terminal often has them where the terminal wrapped the line.

    Raises:
        ValueError: If ``text`` is not hex, or has the wrong length. The message is for the
            user.
    """
    digits = "".join(text.split())
    if not digits or not _HEX.fullmatch(digits):
        raise ValueError("A private key has hex digits only (0-9 and a-f).")
    if len(digits) % 2:
        raise ValueError(
            f"A private key has an even number of hex digits. This one has {len(digits)}."
        )
    return normalize(bytes.fromhex(digits))


def read_private_key_file(path: Path) -> bytes:
    """The 64-byte key in the file at ``path``: hex text, or the raw bytes of a key or a seed.

    ``config export-key --out`` writes hex text. The ``identity.key`` of a node keeps raw
    bytes. The function accepts the two forms. It reads the file as hex text first. A raw
    key is random bytes, and the chance that all of them are hex digits is too small to
    matter. But a hex seed that someone wrote into a key file as text is a real mistake,
    and as raw bytes it would be read as a different key.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If the file does not hold a key. The message is for the user.
    """
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError(f"{path.name} is too large to be a private key.")
    data = path.read_bytes()
    try:
        digits = "".join(data.decode("ascii").split())
    except UnicodeDecodeError:
        digits = ""
    if digits and _HEX.fullmatch(digits):
        return parse_private_key(digits)
    if len(data) in (KEY_BYTES, SEED_BYTES):
        return normalize(data)
    raise ValueError(
        f"{path.name} does not hold a private key. A key file holds the key as hex "
        f"text, or {KEY_BYTES} or {SEED_BYTES} raw bytes."
    )
