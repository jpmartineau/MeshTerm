# SPDX-License-Identifier: Apache-2.0
"""The software node MeshTerm runs on an SPI radio (``meshterm/core/radionode.py``).

Two kinds of test. The first holds the node's contract with its parent and with the
interpreter it runs under — it imports nothing from MeshTerm, reports one status line, and
reads its state files back defensively. The second runs the node for real, on the actual
radio library with a fake radio underneath, and talks to it through MeshTerm's own
companion client: that is the only way to prove the thing this file exists for, which is
that a channel written through MeshTerm is still there after the node restarts.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshterm.core import radionode

NODE_SOURCE = Path(radionode.__file__)


# --- the contract --------------------------------------------------------------------------


def test_node_imports_only_the_standard_library_and_the_radio_library() -> None:
    """The node runs under *another* Python, by path, so it can import nothing of ours."""
    tree = ast.parse(NODE_SOURCE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import only works inside the package"
            names.add((node.module or "").split(".")[0])
    foreign = {n for n in names if n not in sys.stdlib_module_names and n != "__future__"}
    assert foreign == set(), f"the node imports {sorted(foreign)}"
    # And the radio library only by name, at run time — importing it at the top would make
    # a missing library an ImportError traceback instead of the "runtime" report.
    assert radionode.RUNTIME == "openhop_core"


@pytest.mark.parametrize(
    ("lines", "kind"),
    [
        (["GPIO pin 25 is already in use by another process: [Errno 16]"], "busy"),
        (["Failed to setup: Device or resource busy"], "busy"),
        (["Permission denied for GPIO pin 24: ..."], "permission"),
        (["radio.begin() returned False"], "failed"),
        ([], "failed"),
    ],
)
def test_a_failed_begin_is_classified_by_what_the_library_logged(
    lines: list[str], kind: str
) -> None:
    """The library says why a pin failed only in a log line, so that is what is read."""
    assert radionode.classify_begin_failure(lines) == kind


@dataclasses.dataclass
class _Prefs:
    """A stand-in for the library's ``NodePrefs``: one field of each type it has."""

    node_name: str = "pyMC"
    tx_power_dbm: int = 20
    latitude: float = 0.0
    default_scope_key: bytes = b""


def test_prefs_round_trip_through_json() -> None:
    """Every field type ``NodePrefs`` has comes back from ``prefs.json`` as it went in."""
    prefs = _Prefs(node_name="Lakeside", tx_power_dbm=17, latitude=45.5, default_scope_key=b"\x01")
    saved = json.loads(json.dumps(radionode.prefs_to_json(prefs)))
    loaded = _Prefs()
    radionode.apply_saved_prefs(loaded, saved)
    assert loaded == prefs


def test_saved_prefs_take_only_known_fields_of_the_right_type() -> None:
    """A retired field is ignored, and a hand-edited wrong value never reaches the radio."""
    prefs = _Prefs()
    radionode.apply_saved_prefs(
        prefs, {"node_name": "Ridge", "tx_power_dbm": "loud", "retired_knob": 3, "latitude": 1}
    )
    assert prefs == _Prefs(node_name="Ridge", latitude=1.0)
    radionode.apply_saved_prefs(prefs, ["not", "a", "dict"])  # a corrupt file: no change
    assert prefs.node_name == "Ridge"


def test_saved_channels_skip_what_cannot_be_a_channel() -> None:
    """A malformed or cleared entry in ``channels.json`` is skipped, never restored."""
    entries = [
        {"idx": 0, "name": "Public", "secret": "00" * 16},
        {"idx": 1, "name": "", "secret": "00" * 16},  # a cleared slot is not a channel
        {"idx": "two", "name": "Bad", "secret": "00"},
        {"idx": 3, "name": "Hex", "secret": "zz"},
        "garbage",
    ]
    assert radionode.saved_channels(entries) == [(0, "Public", bytes(16))]
    assert radionode.saved_channels(None) == []


def test_radio_kwargs_pass_only_what_the_constructor_accepts() -> None:
    """An older library missing a knob gets the call without it, not a refusal."""
    accepted = {"bus_id", "reset_pin", "en_pins", "frequency", "tx_power"}
    wiring = {"bus_id": 1, "reset_pin": 25, "en_pins": None, "gpio_chip": 0}
    prefs = type(
        "P",
        (),
        {
            "frequency_hz": 869_525_000,
            "bandwidth_hz": 250_000,
            "spreading_factor": 11,
            "coding_rate": 5,
            "tx_power_dbm": 14,
        },
    )()
    assert radionode.radio_kwargs(accepted, wiring, prefs) == {
        "bus_id": 1,
        "reset_pin": 25,
        "frequency": 869_525_000,
        "tx_power": 14,
    }


def _switch(root: Path, name: str, state: str) -> Path:
    """A stand-in for ``/sys/class/leds/<name>``, reading ``state``."""
    (root / name).mkdir(parents=True)
    path = root / name / "brightness"
    path.write_text(state, encoding="ascii")
    return path


def test_board_switches_are_set_and_put_back_as_found(tmp_path: Path) -> None:
    """The Cap's power and pin routing go on for the node and back to how they were after."""
    power = _switch(tmp_path, "ext_5v_out", "0")
    routing = _switch(tmp_path, "ext_usb_gpio_fun", "255")
    previous = radionode.set_leds(["ext_5v_out=1", "ext_usb_gpio_fun=0"], root=tmp_path)
    assert (power.read_text(), routing.read_text()) == ("1", "0")
    radionode.restore_leds(previous)
    assert (power.read_text(), routing.read_text()) == ("0", "255")


def test_a_missing_switch_is_a_wiring_for_another_board(tmp_path: Path) -> None:
    """No such switch means the wiring describes a board this isn't — said, not ignored."""
    with pytest.raises(radionode.NodeError, match="this board has no ext_5v_out switch"):
        radionode.set_leds(["ext_5v_out=1"], root=tmp_path)


def test_a_failed_expander_puts_the_switches_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Cap that isn't there leaves the header unpowered again, not switched on for nothing."""
    power = _switch(tmp_path, "ext_5v_out", "0")
    monkeypatch.setattr(radionode, "LEDS", tmp_path)
    monkeypatch.setattr(radionode, "POWER_SETTLE_S", 0)

    def absent(*_args: object) -> None:
        raise radionode.NodeError("absent", "nothing answers at 0x43 on /dev/i2c-1")

    monkeypatch.setattr(radionode, "drive_pi4io", absent)
    wiring = {"leds": ["ext_5v_out=1"], "pi4io_bus": 1, "pi4io_high": [0]}
    with pytest.raises(radionode.NodeError) as err:
        radionode.prepare_board(wiring)
    assert err.value.kind == "absent"
    assert power.read_text() == "0"


def test_an_expander_on_a_missing_bus_is_named(tmp_path: Path) -> None:
    """No ``/dev/i2c-N`` is its own kind, with the fix the parent knows how to say."""
    with pytest.raises(radionode.NodeError) as err:
        radionode.drive_pi4io(1, 0x43, [0], dev=tmp_path)
    assert err.value.kind == "no-i2c"


def test_a_board_with_no_switches_prepares_nothing() -> None:
    """The AIO's wiring has neither, and its node starts exactly as it always did."""
    assert radionode.prepare_board({"bus_id": 1, "pi4io_bus": -1}) == []


def test_an_identity_is_minted_once_and_then_kept(tmp_path: Path) -> None:
    """A node's key is made the first time and read back every time after."""
    minted = iter([b"\x11" * 32, b"\x22" * 32])
    first = radionode.load_identity_seed(tmp_path, lambda: next(minted))
    again = radionode.load_identity_seed(tmp_path, lambda: next(minted))
    assert first == again == b"\x11" * 32
    assert (tmp_path / "identity.key").read_bytes() == first


@pytest.mark.parametrize(("sf", "symbols"), [(5, 32), (7, 32), (8, 32), (9, 16), (12, 16)])
def test_the_preamble_follows_meshcore(sf: int, symbols: int) -> None:
    """MeshCore sends 32 symbols up to SF8 and 16 above, and the receiver must expect it."""
    assert radionode.preamble_for_sf(sf) == symbols


def test_the_radio_is_built_with_the_meshcore_preamble() -> None:
    """The chip comes up expecting the preamble the mesh sends, never the library's 12."""
    prefs = type(
        "P",
        (),
        {
            "frequency_hz": 910_525_000,
            "bandwidth_hz": 62_500,
            "spreading_factor": 7,
            "coding_rate": 5,
            "tx_power_dbm": 22,
        },
    )()
    kwargs = radionode.radio_kwargs({"preamble_length", "spreading_factor"}, {}, prefs)
    assert kwargs == {"preamble_length": 32, "spreading_factor": 7}


class _Chip:
    """The calls ``apply_preamble`` makes on the driver, recorded in order."""

    STANDBY_RC, HEADER_EXPLICIT, CRC_ON, IQ_STANDARD, RX_CONTINUOUS = 0, 0, 1, 0, 0xFFFFFF

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name: str):  # noqa: ANN204
        return lambda *args: self.calls.append((name, *args))


def test_a_retune_resends_the_preamble_to_the_listening_chip() -> None:
    """Reception keeps its last packet parameters, so a new preamble must be sent to it."""
    chip = _Chip()
    radio = type("R", (), {"preamble_length": 32, "lora": chip})()
    radionode.apply_preamble(radio, 16)
    assert radio.preamble_length == 16
    assert [c[0] for c in chip.calls] == ["setStandby", "setPacketParamsLoRa", "request"]
    assert chip.calls[1][1] == 16
    chip.calls.clear()
    radionode.apply_preamble(radio, 16)  # unchanged: the chip is left listening undisturbed
    assert chip.calls == []


# --- the GPS beside the chip ---------------------------------------------------------------


def _sentence(body: str) -> str:
    """``body`` as a whole NMEA sentence, its checksum computed rather than copied."""
    check = 0
    for byte in body.encode("ascii"):
        check ^= byte
    return f"${body}*{check:02X}"


#: What the Cap's receiver sent on the Cardputer before it had a fix, byte for byte.
_NO_FIX = [
    "$GNRMC,,V,,,,,,,,,,N,V*37",
    "$GNGGA,,,,,,0,00,25.5,,,,,,*64",
    "$GNGSA,A,1,,,,,,,,,,,,,25.5,25.5,25.5,1*01",
]

#: A fix in Montreal, as an RMC: the west longitude is the sign worth checking.
_MONTREAL = _sentence("GNRMC,010203.00,A,4532.1274,N,07342.5219,W,0.1,,051026,,,A,V")


@pytest.mark.parametrize(
    ("line", "fix"),
    [
        # NMEA's own textbook sentences, checksums as published.
        (
            "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A",
            (48.1173, 11.516667),
        ),
        (
            "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47",
            (48.1173, 11.516667),
        ),
        (_MONTREAL, (45.535457, -73.708698)),
        (
            _sentence("GNGGA,010203.00,3352.0000,S,15112.0000,E,1,09,0.9,40,M,,M,,"),
            (-33.866667, 151.2),
        ),
    ],
)
def test_a_valid_fix_is_read_from_either_sentence(line: str, fix: tuple) -> None:
    """RMC and GGA both carry a fix, from any talker, south and west negative."""
    got = radionode.nmea_fix(line)
    assert got is not None and got == pytest.approx(fix, abs=1e-6)


@pytest.mark.parametrize(
    "line",
    [
        *_NO_FIX,
        _MONTREAL.replace("4532", "4533"),  # a byte changed on the wire: checksum fails
        _MONTREAL[:-2],  # cut off inside its checksum
        _sentence("GNRMC,010203.00,A,4560.0000,N,07342.5219,W,,,051026,,,A,V"),  # 60 minutes
        _sentence("GNRMC,010203.00,A,4532.1274,X,07342.5219,W,,,051026,,,A,V"),  # no hemisphere
        _sentence("GNVTG,,,,,,,,,N"),
        "",
        "garbage",
    ],
)
def test_anything_but_a_valid_fix_is_none(line: str) -> None:
    """No fix yet, a damaged sentence, and every other sentence leave the position alone."""
    assert radionode.nmea_fix(line) is None


def _gps_node(lat: float = 0.0, lon: float = 0.0) -> Any:
    """A stand-in companion: all a fix touches is its preferences' two coordinates."""
    return SimpleNamespace(prefs=SimpleNamespace(latitude=lat, longitude=lon))


def test_a_fix_becomes_the_node_s_position_once_per_interval() -> None:
    """The firmware's pacing: the first fix at once, then one per ``gps_interval``."""
    gps = radionode.Gps("/dev/serial0", 115200, {"gps_interval": 30})
    gps.node = _gps_node()
    north = _sentence("GNRMC,010204.00,A,4532.2274,N,07342.5219,W,,,051026,,,A,V")
    gps.feed(("\r\n".join([*_NO_FIX, _MONTREAL]) + "\r\n").encode(), now=100.0)
    assert (gps.node.prefs.latitude, gps.node.prefs.longitude) == pytest.approx(
        (45.535457, -73.708698), abs=1e-6
    )
    assert gps.moved
    gps.feed(f"{north}\r\n".encode(), now=129.0)  # inside the interval: held
    assert gps.node.prefs.latitude == pytest.approx(45.535457, abs=1e-6)
    gps.feed(f"{north}\r\n".encode(), now=130.0)
    assert gps.node.prefs.latitude == pytest.approx(45.537123, abs=1e-6)


def test_an_interval_of_zero_takes_every_fix_once_a_second() -> None:
    """``0`` is the firmware's default and its one second, not a flood of writes."""
    gps = radionode.Gps("/dev/serial0", 115200)
    gps.node = _gps_node()
    gga = _sentence("GNGGA,010203.00,4532.1274,N,07342.5219,W,1,09,0.9,40,M,,M,,")
    gps.feed(f"{_MONTREAL}\r\n".encode(), now=10.0)
    gps.node.prefs.latitude = 0.0  # so a second take would show
    gps.feed(f"{gga}\r\n".encode(), now=10.5)  # the same second's other sentence
    assert gps.node.prefs.latitude == 0.0
    gps.feed(f"{gga}\r\n".encode(), now=11.0)
    assert gps.node.prefs.latitude == pytest.approx(45.535457, abs=1e-6)


def test_a_sentence_split_across_reads_is_read_whole() -> None:
    """A read ends wherever the UART's buffer did, which is rarely at a newline."""
    gps = radionode.Gps("/dev/serial0", 115200)
    gps.node = _gps_node()
    data = f"{_MONTREAL}\r\n".encode()
    gps.feed(data[:20], now=1.0)
    assert not gps.moved
    gps.feed(data[20:], now=1.0)
    assert gps.moved


def test_the_settings_come_and_go_by_the_firmware_s_names() -> None:
    """``gps_enabled`` and ``gps_interval`` in ``prefs.json``; ``gps`` says what *runs*."""
    gps = radionode.Gps("/dev/serial0", 115200, {"gps_enabled": 1, "gps_interval": "45"})
    assert (gps.enabled, gps.interval_s) == (True, 45)
    assert gps.saved() == {"gps_enabled": 1, "gps_interval": 45}
    # Switched on but not (yet) open: a port that failed at start reads off, honestly.
    assert gps.custom_vars() == {"gps": "0", "gps_interval": "45"}
    junk = radionode.Gps("/dev/serial0", 115200, {"gps_enabled": "yes", "gps_interval": None})
    assert (junk.enabled, junk.interval_s) == (False, 0)


@pytest.mark.parametrize(
    ("value", "accepted", "interval"),
    [("60", True, 60), ("0", True, 0), ("99999", True, 86400), ("-5", True, 0), ("soon", False, 7)],
)
def test_the_interval_is_bounded_as_the_firmware_bounds_it(
    value: str, accepted: bool, interval: int
) -> None:
    """``constrain(…, 0, 86400)``, and a value that isn't a number refused outright."""
    gps = radionode.Gps("/dev/serial0", 115200, {"gps_interval": 7})
    assert gps.set_var("gps_interval", value) is accepted
    assert gps.interval_s == interval


def test_switching_on_a_port_that_will_not_open_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The companion answers an error, and the switch stays where it was."""
    gps = radionode.Gps("/dev/serial0", 115200)

    def refuse() -> None:
        raise OSError("[Errno 13] Permission denied: '/dev/serial0'")

    monkeypatch.setattr(gps, "start", refuse)
    assert gps.set_var("gps", "1") is False
    assert gps.enabled is False
    assert gps.set_var("gps", "maybe") is False
    assert gps.set_var("gps", "0") is True and gps.enabled is False


@dataclasses.dataclass
class _GpsPrefs:
    """Enough of ``NodePrefs`` for a position."""

    node_name: str = "MeshTerm"
    latitude: float = 0.0
    longitude: float = 0.0


class _Library:
    """The two custom-variable methods of the library's companion, and its preferences."""

    def __init__(self) -> None:
        self.prefs = _GpsPrefs()
        self._custom_vars: dict[str, str] = {"other": "x"}

    def get_custom_vars(self) -> dict[str, str]:
        return dict(self._custom_vars)

    def set_custom_var(self, name: str, value: str) -> bool:
        self._custom_vars[name] = value
        return True


def test_a_gps_board_lists_its_two_variables_and_saves_them(tmp_path: Path) -> None:
    """Device config finds ``gps`` where firmware puts it, and a change outlives a restart."""
    gps = radionode.Gps("/dev/serial0", 115200)
    node = radionode._persistent_companion(_Library, tmp_path, gps)()
    gps.node = node
    assert node.get_custom_vars() == {"other": "x", "gps": "0", "gps_interval": "0"}
    assert node.set_custom_var("gps_interval", "120") is True
    saved = json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))
    assert saved == {
        "node_name": "MeshTerm",
        "latitude": 0.0,
        "longitude": 0.0,
        "gps_enabled": 0,
        "gps_interval": 120,
    }
    assert node.set_custom_var("gps_interval", "soon") is False  # refused: nothing saved
    assert node.set_custom_var("other", "y") is True  # not the GPS's: the library's own
    assert node.get_custom_vars()["other"] == "y"


def test_a_board_with_no_gps_lists_none(tmp_path: Path) -> None:
    """Firmware lists ``gps`` only where it found a receiver; so does the node."""
    node = radionode._persistent_companion(_Library, tmp_path)()
    assert node.get_custom_vars() == {"other": "x"}
    node._save_prefs()
    assert "gps_enabled" not in json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a POSIX pseudo-terminal")
def test_the_receiver_is_read_from_a_real_terminal() -> None:
    """The port is opened raw at its speed and read on the loop, as the Cap's UART is."""
    master, slave = os.openpty()
    port = os.ttyname(slave)
    gps = radionode.Gps(port, 115200)
    gps.node = _gps_node()

    async def run() -> None:
        assert gps.set_var("gps", "1") is True
        assert gps.custom_vars()["gps"] == "1"
        os.write(master, f"{_NO_FIX[0]}\r\n{_MONTREAL}\r\n".encode())
        for _ in range(50):
            if gps.moved:
                break
            await asyncio.sleep(0.02)
        gps.set_var("gps", "0")

    try:
        asyncio.run(run())
    finally:
        os.close(master)
        os.close(slave)
    assert gps.node.prefs.latitude == pytest.approx(45.535457, abs=1e-6)
    assert not gps.running and gps.custom_vars()["gps"] == "0"
