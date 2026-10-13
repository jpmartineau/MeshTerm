# SPDX-License-Identifier: Apache-2.0
"""The private key of our node: the forms that MeshTerm accepts, and how each device takes it.

A companion exports and imports a 64-byte key. A node of an SPI radio keeps a 32-byte seed.
MeshTerm accepts the two forms from the user and sends only the first form, and these tests
hold each step of that path.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from Crypto.PublicKey import ECC
from meshcore import EventType
from meshcore.events import Event

from meshterm.core import spiradio
from meshterm.core.config import SpiWiring
from meshterm.core.connection import DeviceCommandError, MeshCoreDevice
from meshterm.core.privatekey import (
    expand_seed,
    normalize,
    parse_private_key,
    read_private_key_file,
)

#: The base point of Ed25519 (RFC 8032, section 5.1).
_GX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_GY = 46316835694926478169428394003475163141307993866256225615783033603165251855960


# --- the forms -----------------------------------------------------------------------------


def test_an_expanded_seed_has_the_public_key_of_the_seed() -> None:
    """The firmware gets the public key from the first half of the key, with no more clamp.

    That point must be the public key that Ed25519 makes from the seed. If it is not, the
    import gives the device a different identity from the one the user exported.
    """
    seed = bytes(range(32))
    key = expand_seed(seed)
    assert len(key) == 64
    point = ECC.EccPoint(_GX, _GY, curve="Ed25519") * int.from_bytes(key[:32], "little")
    expected = ECC.construct(curve="Ed25519", seed=seed).pointQ
    assert (point.x, point.y) == (expected.x, expected.y)


def test_an_expanded_seed_is_clamped_as_the_firmware_clamps_it() -> None:
    """The three low bits are clear, the top bit is clear, and the bit below it is set."""
    key = expand_seed(b"\xff" * 32)
    assert key[0] & 0x07 == 0
    assert key[31] & 0x80 == 0
    assert key[31] & 0x40


def test_a_key_passes_as_it_is_and_a_seed_is_expanded() -> None:
    """The device always gets 64 bytes."""
    key = bytes.fromhex("cd" * 64)
    assert normalize(key) == key
    assert normalize(key[:32]) == expand_seed(key[:32])


def test_a_pasted_seed_stays_a_seed() -> None:
    """The parser returns the form that the user gave. Each device takes the form it keeps."""
    assert parse_private_key("ab" * 32) == bytes.fromhex("ab" * 32)


def test_a_pasted_key_can_have_line_breaks_and_capitals() -> None:
    """A key that the user copies from a terminal has the breaks where the terminal wrapped it."""
    pasted = "CD" * 32 + "\n  " + "cd" * 32 + "\n"
    assert parse_private_key(pasted) == bytes.fromhex("cd" * 64)


@pytest.mark.parametrize(
    "text,problem",
    [
        ("", "hex digits only"),
        ("not a key", "hex digits only"),
        ("abc", "even number"),
        ("ab" * 40, "This one has 40"),
    ],
    ids=["empty", "not-hex", "odd", "wrong-length"],
)
def test_a_bad_key_says_why(text: str, problem: str) -> None:
    """The message is for the user, who must correct what they pasted."""
    with pytest.raises(ValueError, match=problem):
        parse_private_key(text)


@pytest.mark.parametrize(
    "content,expected",
    [
        (("cd" * 64 + "\n").encode(), bytes.fromhex("cd" * 64)),
        (bytes.fromhex("cd" * 64), bytes.fromhex("cd" * 64)),
        (bytes(range(32)), bytes(range(32))),
        (("ab" * 32).encode(), bytes.fromhex("ab" * 32)),
    ],
    ids=["hex-text", "raw-key", "raw-seed", "seed-as-hex-text"],
)
def test_a_key_file_can_hold_hex_text_or_raw_bytes(
    tmp_path: Path, content: bytes, expected: bytes
) -> None:
    """``export-key --out`` writes hex text, and the ``identity.key`` of a node holds raw bytes.

    The key comes back in its own form: a seed stays a seed, for the device to place.

    A seed that someone wrote into a file as hex text is 64 bytes on disk. As raw bytes, it
    would be a different key with no error, so the file is read as text first.
    """
    path = tmp_path / "node.key"
    path.write_bytes(content)
    assert read_private_key_file(path) == expected


def test_a_file_that_holds_no_key_says_so(tmp_path: Path) -> None:
    """A file of the wrong size or contents is refused, with its name in the message."""
    path = tmp_path / "notes.bin"
    path.write_bytes(b"\x00" * 10)
    with pytest.raises(ValueError, match="notes.bin does not hold a private key"):
        read_private_key_file(path)
    path.write_bytes(b"\x00" * 5000)
    with pytest.raises(ValueError, match="too large"):
        read_private_key_file(path)


# --- the devices ---------------------------------------------------------------------------


def _device_that_answers(event: Event) -> tuple[MeshCoreDevice, list[bytes]]:
    """A device whose client answers each command with ``event``, and the frames it got."""
    sent: list[bytes] = []

    async def send(data: bytes, expected: list, timeout: float | None = None) -> Event:
        sent.append(data)
        assert event.type in expected, "the device must wait for this answer"
        return event

    device = MeshCoreDevice(port="mock")
    device._mc = SimpleNamespace(commands=SimpleNamespace(send=send))
    return device, sent


async def test_a_firmware_import_sends_the_key() -> None:
    """The command is 0x18 and the 64 bytes of the key."""
    device, sent = _device_that_answers(Event(EventType.OK, {}))
    await device.import_private_key("cd" * 64)
    assert sent == [b"\x18" + bytes.fromhex("cd" * 64)]


async def test_a_firmware_import_expands_a_seed() -> None:
    """A firmware companion accepts only 64 bytes, so it gets a seed expanded."""
    device, sent = _device_that_answers(Event(EventType.OK, {}))
    await device.import_private_key("ab" * 32)
    assert sent == [b"\x18" + expand_seed(bytes.fromhex("ab" * 32))]


async def test_a_firmware_with_the_import_off_says_so() -> None:
    """The firmware answers "disabled". Before, MeshTerm waited for its timeout and said nothing."""
    device, _sent = _device_that_answers(Event(EventType.DISABLED, {}))
    with pytest.raises(DeviceCommandError, match="does not accept an imported key"):
        await device.import_private_key("cd" * 64)


async def test_an_spi_node_gets_its_imported_key_as_a_file(tmp_path: Path) -> None:
    """The node of the radio library refuses an import, so MeshTerm writes its key file.

    The node reads the file when it starts again, so the device says that the import
    restarts it.
    """
    device = spiradio.SpiRadioDevice(SpiWiring(), tmp_path / "state")
    assert device.import_restarts
    key = expand_seed(bytes(range(32)))
    await device.import_private_key(key.hex())
    path = tmp_path / "state" / "identity.key"
    assert path.read_bytes() == key
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_an_spi_node_keeps_an_imported_seed_as_a_seed(tmp_path: Path) -> None:
    """The radio library keeps keys as seeds, and a seed is the more complete form.

    Thus the node saves an imported seed as it is, and does not expand it (JP, 2026-10-13).
    """
    device = spiradio.SpiRadioDevice(SpiWiring(), tmp_path / "state")
    await device.import_private_key("ab" * 32)
    assert (tmp_path / "state" / "identity.key").read_bytes() == bytes.fromhex("ab" * 32)


def test_a_firmware_companion_uses_its_new_key_at_once() -> None:
    """Only a node that MeshTerm runs must start again to use the key."""
    assert MeshCoreDevice.import_restarts is False


# --- the run log ---------------------------------------------------------------------------


def test_a_v18_database_forgets_the_keys_that_import_key_stored(tmp_path: Path) -> None:
    """Before v19, the run of ``config import-key`` stored its key. The migration removes it.

    The other runs and the other arguments stay as they were.
    """
    import json

    from meshterm.persistence import db as db_module

    path = tmp_path / "old.db"
    conn = db_module.connect(path)
    conn.execute("UPDATE schema_meta SET value = '18' WHERE key = 'version'")
    for tool, args in (
        ("config", {"ops": [["import_key", "cd" * 64]]}),
        ("config", {"ops": [["set", "name", "Lakeside"]]}),
        ("trace", {"to": "Lakeside"}),
    ):
        conn.execute(
            "INSERT INTO runs (tool, args_json, started_at) VALUES (?, ?, '2026-10-01')",
            (tool, json.dumps(args)),
        )
    conn.commit()
    conn.close()

    conn = db_module.connect(path)
    stored = [json.loads(row[0]) for row in conn.execute("SELECT args_json FROM runs ORDER BY id")]
    version = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    conn.close()
    assert stored == [
        {"ops": [["import_key", "<secret>"]]},
        {"ops": [["set", "name", "Lakeside"]]},
        {"to": "Lakeside"},
    ]
    assert int(version[0]) >= 19
