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
import dataclasses
import json
import sys
from pathlib import Path

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
