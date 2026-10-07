# SPDX-License-Identifier: Apache-2.0
"""Region scopes: the key and code maths, the store of known regions, and where scopes are kept.

The tests compare the maths with the definitions of the firmware itself (``TransportKeyStore``,
``TransportKey::calcTransportCode``). The tests write these definitions again, independently.
Thus a fault in :mod:`meshterm.core.regions` cannot pass because the code agrees with itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshterm.core import regions
from meshterm.core.models import Observation, utcnow
from meshterm.core.region_store import RegionStore
from meshterm.core.regions import (
    UNSCOPED,
    RegionNameError,
    Scope,
    frame_scope,
    parse_codes,
    parse_region_list,
    region_key,
    resolve,
    scope_body,
    transport_code,
    validate,
)
from meshterm.persistence.repository import Repository

#: A channel-text payload: channel hash, 2-byte MAC, and ciphertext. The content is arbitrary.
_PAYLOAD = bytes.fromhex("a71c2d00112233445566778899aabbccddeeff")
_GRP_TXT = 5


def _firmware_code(name: str, payload_type: int, payload: bytes) -> int:
    """The transport code that the firmware computes, written in full steps."""
    key = hashlib.sha256(("#" + name).encode("utf-8")).digest()[:16]
    mac = hmac.new(key, bytes([payload_type]) + payload, hashlib.sha256).digest()
    code = mac[0] | (mac[1] << 8)
    return {0: 1, 0xFFFF: 0xFFFE}.get(code, code)


def _scoped_raw(name: str, payload: bytes = _PAYLOAD) -> dict:
    """The raw payload of an RX-log frame for a flood with the scope ``name``.

    The payload has the form that the library parses.
    """
    code = _firmware_code(name, _GRP_TXT, payload)
    return {
        "route_typename": "TC_FLOOD",
        "payload_type": _GRP_TXT,
        "payload_typename": "GRP_TXT",
        "pkt_payload": payload,
        "transport_code": code.to_bytes(2, "little").hex() + "0000",
    }


# -- the maths ---------------------------------------------------------------------------


def test_key_is_the_hashed_hashtag_name_either_way_it_is_written() -> None:
    """``yul`` and ``#yul`` are one region. The key is the hash of ``#yul`` as UTF-8."""
    expected = hashlib.sha256(b"#yul").digest()[:16]
    assert region_key("yul") == region_key("#yul") == region_key("  #yul ") == expected
    assert region_key("montréal") == hashlib.sha256("#montréal".encode()).digest()[:16]


def test_code_matches_the_firmware_definition() -> None:
    """The code is the HMAC of the type byte and the payload.

    It uses the first two bytes, little-endian.
    """
    code = transport_code(region_key("yul"), scope_body(_GRP_TXT, _PAYLOAD))
    assert code == _firmware_code("yul", _GRP_TXT, _PAYLOAD)


def test_reserved_codes_step_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """0x0000 and 0xFFFF are reserved. The firmware sends 0x0001 and 0xFFFE instead."""
    for digest, expected in ((b"\x00\x00", 0x0001), (b"\xff\xff", 0xFFFE)):
        fake = SimpleNamespace(digest=lambda d=digest: d + bytes(30))
        monkeypatch.setattr(regions.hmac, "new", lambda *a, f=fake, **k: f)
        assert transport_code(bytes(16), b"x") == expected


def test_codes_field_splits_little_endian() -> None:
    """The 4-byte field is two little-endian uint16 values. Invalid data parses to nothing."""
    assert parse_codes("341200ff") == (0x1234, 0xFF00)
    assert parse_codes(None) is None
    assert parse_codes("12") is None
    assert parse_codes("zzzzzzzz") is None


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "needs a name"),
        ("#", "needs a name"),
        ("*", "every region"),
        ("$secret", "private"),
        ("é" * 16, "30 bytes"),
        ("north shore", "no spaces"),
        ("a,b", "no spaces or commas"),
    ],
)
def test_names_the_firmware_would_refuse_are_refused(name: str, message: str) -> None:
    """MeshTerm refuses at the start a name that it can store but that never matches.

    This includes a name that never lists back.
    """
    with pytest.raises(RegionNameError, match=message):
        validate(name)


def test_valid_names_come_back_bare() -> None:
    """Validation also normalizes the name. The ``#`` belongs to the key, not to the name."""
    assert validate("#yul-nord") == "yul-nord"
    assert validate("x" * 30) == "x" * 30


def test_resolution_finds_the_region_whose_key_reproduces_the_code() -> None:
    """A code does not name a region. A test of the known names finds the region."""
    raw = _scoped_raw("harbour")
    code = parse_codes(raw["transport_code"])[0]
    body = scope_body(_GRP_TXT, _PAYLOAD)
    assert resolve(["lakeside", "#harbour"], body, code) == "harbour"
    assert resolve(["lakeside", "*"], body, code) is None


def test_frame_scope_reads_the_three_states_and_ignores_direct_frames() -> None:
    """A plain flood is unscoped. A scoped flood has a region or an unknown scope.

    A direct packet has no scope.
    """
    assert frame_scope({"route_typename": "FLOOD"}, ["harbour"]) is UNSCOPED
    assert frame_scope({"route_typename": "DIRECT"}, ["harbour"]) is None
    assert frame_scope({"route_typename": "TC_DIRECT"}, ["harbour"]) is None
    assert frame_scope({}, ["harbour"]) is None
    raw = _scoped_raw("harbour")
    code = raw["transport_code"][:4]
    code = f"{int.from_bytes(bytes.fromhex(code), 'little'):04x}"
    assert frame_scope(raw, ["harbour"]) == Scope("scoped", "harbour", code)
    assert frame_scope(raw, ["lakeside"]) == Scope("unknown", None, code)


def test_a_repeaters_region_list_parses_bare_with_the_wildcard_kept() -> None:
    """``*`` is first when the repeater relays unscoped floods. Names are bare.

    The parse removes duplicates and blanks.
    """
    assert parse_region_list("*,#lakeside,harbour,,harbour\x00") == ["*", "lakeside", "harbour"]
    assert parse_region_list(None) == []


# -- the store ---------------------------------------------------------------------------


def test_store_learns_persists_and_orders_chosen_names_first(tmp_path: Path) -> None:
    """Names that the user chose resolve before names that only a repeater mentioned."""
    path = tmp_path / "regions.json"
    store = RegionStore(path)
    store.learn_carried("aabbccddeeff0011", ["*", "harbour", "lakeside"])
    store.learn("lakeside", "typed")
    assert store.names() == ["lakeside", "harbour"]
    assert store.carriers("harbour") == ("aabbccddeeff",)
    assert store.carried_by("AABBCCDDEEFF") == ["lakeside", "harbour"]

    reopened = RegionStore(path)
    assert reopened.names() == ["lakeside", "harbour"]
    assert reopened.get("#lakeside").sources == ("repeater", "typed")


def test_a_repeaters_new_list_replaces_what_it_said_before(tmp_path: Path) -> None:
    """The store forgets a region that only one repeater taught.

    This occurs when that repeater stops naming the region.
    """
    store = RegionStore(tmp_path / "regions.json")
    store.learn_carried("aabbccddeeff", ["harbour", "lakeside"])
    store.learn("lakeside", "typed")
    store.learn_carried("aabbccddeeff", ["harbour"])
    assert store.names() == ["lakeside", "harbour"]
    assert store.carriers("lakeside") == ()
    store.learn_carried("aabbccddeeff", [])
    assert store.names() == ["lakeside"]


def test_channel_scope_is_kept_by_identity_and_teaches_the_name(tmp_path: Path) -> None:
    """A set of the scope of a channel stores the region. A clear of it leaves the name known."""
    store = RegionStore(tmp_path / "regions.json")
    store.set_channel_scope("chan-1", "#harbour")
    assert store.channel_scope("chan-1") == "harbour"
    assert store.get("harbour").sources == ("channel",)
    with pytest.raises(RegionNameError):
        store.set_channel_scope("chan-1", "two words")
    store.set_channel_scope("chan-1", None)
    assert store.channel_scope("chan-1") is None
    assert RegionStore(tmp_path / "regions.json").names() == ["harbour"]


def test_forgetting_a_region_clears_the_channels_scoped_to_it(tmp_path: Path) -> None:
    """A channel never keeps a scope for a region that the store does not know."""
    store = RegionStore(tmp_path / "regions.json")
    store.set_channel_scope("chan-1", "harbour")
    store.forget("harbour")
    assert store.names() == []
    assert store.channel_scope("chan-1") is None


def test_scope_of_resolves_against_names_learned_after_the_frame(tmp_path: Path) -> None:
    """The cache is cleared when a name arrives, so a frame that was heard earlier now resolves."""
    store = RegionStore(tmp_path / "regions.json")
    raw = _scoped_raw("harbour")
    assert store.scope_of(raw).state == "unknown"
    store.learn("harbour", "typed")
    assert store.scope_of(raw).region == "harbour"
    assert store.scope_of({"route_typename": "FLOOD"}) is UNSCOPED
    assert store.scope_of({"route_typename": "DIRECT"}) is None
    assert store.scope_of(None) is None


# -- persistence -------------------------------------------------------------------------


def test_a_scoped_frame_keeps_what_it_needs_to_resolve_from_history(tmp_path: Path) -> None:
    """The code and its HMAC input stay in the database.

    For other frames, MeshTerm stores nothing.
    """
    repo = Repository(tmp_path / "meshterm.db")
    run = repo.start_run("monitor", {})
    when = utcnow()
    scoped = Observation(
        node=None, name=None, kind="packet", path="", observed_at=when, raw=_scoped_raw("harbour")
    )
    plain = Observation(
        node=None,
        name=None,
        kind="packet",
        path="",
        observed_at=when + timedelta(seconds=1),
        raw={"route_typename": "FLOOD", "payload_typename": "GRP_TXT", "pkt_payload": _PAYLOAD},
    )
    repo.record_observation(run, scoped)
    repo.record_observation(run, plain)
    replayed = repo.recent_observations(since=when - timedelta(minutes=1))
    assert [o.raw.get("route_typename") for o in replayed] == ["TC_FLOOD", "FLOOD"]
    assert replayed[0].raw["scope_body"] == scope_body(_GRP_TXT, _PAYLOAD).hex()
    assert "scope_body" not in replayed[1].raw and "transport_code" not in replayed[1].raw

    store = RegionStore(tmp_path / "regions.json")
    store.learn("harbour", "repeater", repeater="aabbccddeeff")
    assert store.scope_of(replayed[0].raw).region == "harbour"


# -- the device ----------------------------------------------------------------------------


class _Commands:
    """A stand-in for the meshcore ``commands`` that records what MeshTerm sent."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send(self, frame, expected):  # noqa: ANN001, ANN201
        self.calls.append(("send", bytes(frame)))

    async def set_flood_scope(self, key):  # noqa: ANN001, ANN201
        self.calls.append(("scope", key))

    async def reset_flood_scope(self):  # noqa: ANN201
        self.calls.append(("reset",))

    async def force_unscoped(self):  # noqa: ANN201
        self.calls.append(("unscoped",))


def _device(commands: _Commands):  # noqa: ANN202
    from meshterm.core.connection import MeshCoreDevice

    device = object.__new__(MeshCoreDevice)
    device._mc = SimpleNamespace(commands=commands)
    return device


def test_default_scope_frame_is_bare_and_byte_padded() -> None:
    """The frame is ``[63][name, 31 bytes NUL-padded][key16]``.

    The name is bare and in UTF-8 bytes. The frame has 48 bytes in all.
    """
    commands = _Commands()
    device = _device(commands)
    asyncio.run(device.set_default_flood_scope("#montréal"))
    frame = commands.calls[-1][1]
    name = "montréal".encode()
    assert frame == bytes([63]) + name + bytes(31 - len(name)) + region_key("montréal")
    assert len(frame) == 48
    asyncio.run(device.set_default_flood_scope(""))
    assert commands.calls[-1] == ("send", bytes([63]))
    with pytest.raises(RegionNameError):
        asyncio.run(device.set_default_flood_scope("x" * 31))


def test_session_scope_sends_the_key_the_override_or_the_reset() -> None:
    """A name sends its key. ``*`` forces unscoped. ``None`` goes back to the default.

    MeshTerm makes the frame as command 54 of the firmware. It never uses the
    ``reset_flood_scope`` and ``force_unscoped`` of the library, because an older meshcore 2.3
    does not have them.
    """
    commands = _Commands()
    device = _device(commands)
    asyncio.run(device.set_flood_scope("#harbour"))
    asyncio.run(device.set_flood_scope("*"))
    asyncio.run(device.set_flood_scope(None))
    assert commands.calls == [
        ("send", bytes([54, 0]) + region_key("harbour")),
        ("send", bytes([54, 1])),
        ("send", bytes([54, 0])),
    ]


def test_mock_device_mirrors_scope_and_answers_regions_from_repeaters() -> None:
    """The mock stores bare names. Only a repeater answers the request for regions."""
    from meshterm.core.connection import DeviceCommandError, MockDevice

    async def run() -> None:
        device = MockDevice()
        await device.connect()
        await device.set_default_flood_scope("#harbour")
        assert await device.get_default_flood_scope() == "harbour"
        await device.set_flood_scope("*")
        assert device._send_scope == "*"
        contacts = await device.get_contacts()
        repeaters = [c for c in contacts if device._neighbour_tables.get(device._mock_key(c)[:8])]
        others = [c for c in contacts if c not in repeaters]
        assert repeaters and others
        assert (await device.request_regions(repeaters[0]))[0] == "*"
        with pytest.raises(DeviceCommandError):
            await device.request_regions(others[0])

    asyncio.run(run())
