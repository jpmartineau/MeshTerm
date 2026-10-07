# SPDX-License-Identifier: Apache-2.0
"""The MeshTerm half of the SPI radio: wiring, the search for the Python of the node, and its life.

:mod:`tests.test_radionode` tests the node itself. Here a stand-in node script plays the
part of the node. It prints one status line on stdout, then waits for stdin to close. Thus
these tests can run the protocol between the two processes, the words of the errors, and
the steps of the shutdown on any platform.
"""

from __future__ import annotations

import io
import json
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core import spiradio
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import DeviceProfile, Settings, SpiWiring
from meshterm.core.device_store import DeviceStore
from meshterm.core.discovery import TRANSPORT_SPI, DiscoveredDevice, spi_device
from meshterm.persistence.repository import Repository

# --- wiring --------------------------------------------------------------------------------


def test_the_default_wiring_is_the_uconsole_aio() -> None:
    """A profile with no ``spi`` table is the AIO v1. The defaults describe this board."""
    wiring = SpiWiring()
    assert (wiring.bus_id, wiring.reset_pin, wiring.busy_pin, wiring.irq_pin) == (1, 25, 24, 26)
    assert wiring.use_dio2_rf and wiring.use_dio3_tcxo and wiring.en_pins == ()
    assert wiring.spidev == "/dev/spidev1.0"


def test_a_wiring_table_states_only_what_differs() -> None:
    """The only difference of the AIO v2 is its power-enable pin. Nothing else changes."""
    wiring = SpiWiring.from_toml({"en_pins": [27]})
    assert wiring == SpiWiring(en_pins=(27,))


@pytest.mark.parametrize(
    ("table", "complaint"),
    [
        ({"irq_pn": 22}, "unknown SPI wiring key(s): irq_pn"),
        ({"reset_pin": "25"}, "reset_pin = '25' is not a int"),
        ({"use_dio2_rf": 1}, "use_dio2_rf = 1 is not a bool"),
        ({"en_pins": 27}, "en_pins = 27 is not a tuple"),
        ({"busy_pin": True}, "busy_pin = True is not a int"),
    ],
)
def test_a_wiring_mistake_is_refused_not_ignored(table: dict, complaint: str) -> None:
    """If the code accepted a misspelt pin, a default would stay, and the radio would be deaf."""
    with pytest.raises(ValueError, match=complaint.replace("(", r"\(").replace(")", r"\)")):
        SpiWiring.from_toml(table)


def test_a_board_s_switches_and_expander_come_from_the_table() -> None:
    """``leds`` holds switch strings and ``pi4io_high`` holds pins. The code checks each list."""
    wiring = SpiWiring.from_toml(
        {"leds": ["ext_5v_out=1"], "pi4io_bus": 1, "pi4io_address": 0x44, "pi4io_high": [0, 3]}
    )
    assert wiring == SpiWiring(
        leds=("ext_5v_out=1",), pi4io_bus=1, pi4io_address=0x44, pi4io_high=(0, 3)
    )


@pytest.mark.parametrize(
    ("table", "complaint"),
    [
        ({"leds": [1]}, "leds = [1] is not a tuple"),
        ({"pi4io_high": ["0"]}, "pi4io_high = ['0'] is not a tuple"),
        ({"leds": ["ext_5v_out"]}, "leds entry 'ext_5v_out' is not name=brightness"),
        ({"leds": ["ext_5v_out=on"]}, "leds entry 'ext_5v_out=on' is not name=brightness"),
    ],
)
def test_a_malformed_switch_is_refused(table: dict, complaint: str) -> None:
    """A switch that the node cannot set would fail at connect. The error must name the file."""
    with pytest.raises(ValueError, match=complaint.replace("[", r"\[").replace("]", r"\]")):
        SpiWiring.from_toml(table)


def test_the_retired_preamble_key_is_refused_with_its_reason() -> None:
    """A config with the old fixed preamble gets the reason. The radio does not stay deaf."""
    with pytest.raises(ValueError, match="preamble_length is no longer a wiring key"):
        SpiWiring.from_toml({"preamble_length": 12})


def test_a_profile_with_an_spi_table_is_an_spi_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``spi`` table means the transport, in the same way that ``host`` means TCP."""
    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        textwrap.dedent(
            """
            [profiles.aio2.spi]
            en_pins = [27]

            [profiles.aio]
            transport = "spi"
            """
        ),
        encoding="utf-8",
    )
    profiles = Settings.load().profiles
    assert profiles["aio2"].is_spi and profiles["aio2"].spi == SpiWiring(en_pins=(27,))
    assert profiles["aio"].is_spi and profiles["aio"].spi == SpiWiring()


def test_a_bad_wiring_table_names_its_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wiring error names the profile that has it, and is not a bare key."""
    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[profiles.aio.spi]\nirq = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[profiles\.aio\.spi\]"):
        Settings.load()


def test_state_lives_under_the_spi_device_not_the_profile(tmp_path: Path) -> None:
    """One radio is one node, however MeshTerm reached it. Thus MeshTerm files it by device node."""
    assert spiradio.state_dir(tmp_path, SpiWiring()) == tmp_path / "radio" / "spidev1.0"
    assert spiradio.state_dir(tmp_path, SpiWiring(bus_id=0, cs_id=1)).name == "spidev0.1"


def test_an_spi_radio_lists_like_a_device_on_its_node() -> None:
    """The device node names the picker row. It is also the identity that MeshTerm remembers."""
    device = spi_device(SpiWiring(), name="aio")
    assert device.transport == TRANSPORT_SPI and device.is_spi
    assert device.target == "/dev/spidev1.0"
    assert device.stable_id == "spi:/dev/spidev1.0"
    assert device.label == "aio (/dev/spidev1.0)"
    assert spi_device(SpiWiring()).label == "SPI radio (/dev/spidev1.0)"


# --- choosing the radio --------------------------------------------------------------------


@pytest.fixture()
def contexts(tmp_path: Path):  # noqa: ANN201 - a factory fixture
    """Build contexts over a temporary config directory, and close their databases after."""
    made: list[AppContext] = []

    def make(**kwargs: object) -> AppContext:
        profiles = kwargs.pop("profiles", {})
        settings = Settings(
            config_dir=tmp_path,
            db_path=tmp_path / "spi.db",
            profiles=profiles,  # type: ignore[arg-type]
        )
        ctx = AppContext(
            console=Console(file=io.StringIO()),
            settings=settings,
            repo=Repository(settings.db_path),
            device_store=DeviceStore(tmp_path / "devices.json"),
            admin_store=AdminStore(tmp_path / "admin.json"),
            **kwargs,  # type: ignore[arg-type]
        )
        made.append(ctx)
        return ctx

    yield make
    for ctx in made:
        ctx.repo.close()


def test_spi_flag_takes_the_spi_profile_wiring(contexts) -> None:  # noqa: ANN001
    """``--spi`` means the radio on this machine, wired as its profile says (if it has one)."""
    wired = SpiWiring(en_pins=(27,))
    profiles = {"aio2": DeviceProfile(name="aio2", transport="spi", spi=wired)}
    assert contexts(spi_override=True, profiles=profiles).resolve_spi() == wired
    assert contexts(spi_override=True).resolve_spi() == SpiWiring()
    assert contexts(spi_override=True).active_transport == "spi"


def test_spi_flag_takes_the_one_radio_attached(contexts, monkeypatch) -> None:  # noqa: ANN001
    """The common case: one radio on the bus. --spi is enough, with or without a profile."""
    monkeypatch.setattr(spiradio, "spi_present", lambda w: w.spidev == "/dev/spidev1.0")
    elsewhere = SpiWiring(bus_id=0, reset_pin=17)  # a profile for a radio that is not attached
    profiles = {"hat": DeviceProfile(name="hat", transport="spi", spi=elsewhere)}
    assert contexts(spi_override=True, profiles=profiles).resolve_spi() == SpiWiring()
    assert contexts(spi_override=True).resolve_spi() == SpiWiring()


def test_spi_flag_refuses_to_guess_between_two_attached_radios(
    contexts,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With two radios on the bus, --spi names neither. The error lists them and asks for -p."""
    from meshterm.core.selection import DeviceSelectionError

    monkeypatch.setattr(spiradio, "spi_present", lambda w: True)
    hat = SpiWiring(bus_id=0, reset_pin=17)
    profiles = {"hat": DeviceProfile(name="hat", transport="spi", spi=hat)}
    with pytest.raises(DeviceSelectionError) as err:
        contexts(spi_override=True, profiles=profiles).resolve_spi()
    message = str(err.value)
    assert "Several radios are attached to the SPI bus" in message
    assert "hat" in message and "/dev/spidev0.0" in message
    assert "(no profile)" in message and "/dev/spidev1.0" in message
    assert "-p <PROFILE>" in message
    # A profile still says which radio, however many radios there are.
    ctx = contexts(profile=profiles["hat"], profiles=profiles)
    assert ctx.resolve_spi() == hat


def test_spi_flag_refuses_two_profiles_with_nothing_attached(
    contexts,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no radio to count, two SPI profiles make --spi as ambiguous as two radios."""
    from meshterm.core.selection import DeviceSelectionError

    monkeypatch.setattr(spiradio, "spi_present", lambda w: False)
    profiles = {
        "aio": DeviceProfile(name="aio", transport="spi"),
        "hat": DeviceProfile(name="hat", transport="spi", spi=SpiWiring(bus_id=0)),
    }
    with pytest.raises(DeviceSelectionError, match="Several SPI profiles are configured"):
        contexts(spi_override=True, profiles=profiles).resolve_spi()


def test_another_named_radio_is_not_the_spi_radio(contexts) -> None:  # noqa: ANN001
    """A port, an address, or a host that the user names means that the session is for it."""
    for override in (
        {"port_override": "COM7"},
        {"tcp_override": "10.0.0.2"},
        {"ble_override": "AA"},
    ):
        assert contexts(**override).resolve_spi() is None


def test_a_listed_radio_is_wired_by_the_profile_on_its_node(contexts) -> None:  # noqa: ANN001
    """A remembered or listed radio is known by its node. The profile of the node gives the pins."""
    other = SpiWiring(bus_id=0, reset_pin=17)
    profiles = {"hat": DeviceProfile(name="hat", transport="spi", spi=other)}
    ctx = contexts(profiles=profiles)
    assert ctx.spi_wiring_for("/dev/spidev0.0") == other
    assert ctx.spi_wiring_for("/dev/spidev1.0") == SpiWiring()


# --- radios that need no profile -----------------------------------------------------------


@pytest.fixture()
def cardputer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine with the SPI node of the Cardputer Zero, and its board marker in ``tmp_path``.

    Returns the marker. A test removes the marker to make the machine another Raspberry Pi
    that has the same ``/dev/spidev0.1``.
    """
    marker = tmp_path / "ext_5v_out"
    marker.mkdir()
    monkeypatch.setattr(spiradio, "spi_present", lambda w: w.spidev == "/dev/spidev0.1")
    monkeypatch.setattr(
        spiradio,
        "BUILTIN_RADIOS",
        (
            spiradio.BuiltinRadio("", SpiWiring()),
            spiradio.BuiltinRadio("Cap LoRa-1262", spiradio.CARDPUTER_ZERO_CAP, marker=str(marker)),
        ),
    )
    return marker


def test_the_cardputer_cap_is_listed_with_no_profile(cardputer: Path) -> None:
    """On a Cardputer Zero, the Cap is a radio like the AIO: it is present, named, and wired."""
    radios = spiradio.spi_radios({})
    assert [(d.port, d.name) for d in radios] == [("/dev/spidev0.1", "Cap LoRa-1262")]
    assert radios[0].spi == spiradio.CARDPUTER_ZERO_CAP


def test_a_bare_spidev_node_is_not_a_cardputer(cardputer: Path) -> None:
    """``spidev0.1`` is on each Pi that has SPI on. Only the switch of the board says Cardputer."""
    cardputer.rmdir()
    assert spiradio.spi_radios({}) == []
    assert spiradio.builtin_wiring("/dev/spidev0.1") is None


def test_the_cap_reports_itself_by_name(cardputer: Path) -> None:
    """The model of the node is the name of the board: the Cap, also under a changed profile."""
    assert spiradio.board_name(spiradio.CARDPUTER_ZERO_CAP) == "Cap LoRa-1262"
    tweaked = replace(spiradio.CARDPUTER_ZERO_CAP, use_dio3_tcxo=False)
    assert spiradio.board_name(tweaked) == "Cap LoRa-1262"
    assert spiradio.board_name(SpiWiring()) == ""  # the node of the AIO: no board name to give
    cardputer.rmdir()
    assert spiradio.board_name(spiradio.CARDPUTER_ZERO_CAP) == ""  # another Pi


#: Each mark of a uConsole, as the CM5 uConsole of JP showed them.
_UCONSOLE_MARKS = (
    *sorted(spiradio.UCONSOLE_TREE),
    spiradio.UCONSOLE_KEYBOARD,
)


@pytest.fixture()
def uconsole(monkeypatch: pytest.MonkeyPatch) -> set:
    """A uConsole with the SPI node of the AIO: each mark is present, and a test can remove one."""
    marks: set = set(_UCONSOLE_MARKS)
    tree = {"raspberrypi,5-compute-module", "brcm,bcm2712"}
    usb = {("1d6b", "0002", "Linux", "xHCI Host Controller")}
    monkeypatch.setattr(spiradio, "spi_present", lambda w: w.spidev == "/dev/spidev1.0")
    monkeypatch.setattr(
        spiradio,
        "device_tree_compatibles",
        lambda: frozenset(tree | {m for m in marks if isinstance(m, str)}),
    )
    monkeypatch.setattr(
        spiradio,
        "usb_devices",
        lambda: frozenset(usb | {m for m in marks if isinstance(m, tuple)}),
    )
    return marks


def test_the_aio_is_named_on_a_uconsole(uconsole: set) -> None:
    """When each mark of a uConsole is present, the AIO is the uConsole AIO."""
    radios = spiradio.spi_radios({})
    assert [(d.port, d.name) for d in radios] == [("/dev/spidev1.0", "uConsole AIO")]
    assert spiradio.board_name(SpiWiring()) == "uConsole AIO"


@pytest.mark.parametrize("missing", _UCONSOLE_MARKS, ids=str)
def test_one_missing_mark_is_enough_to_doubt_it(uconsole: set, missing: object) -> None:
    """If any one mark is missing, MeshTerm lists the radio with the AIO wiring, with no name."""
    uconsole.discard(missing)
    radios = spiradio.spi_radios({})
    assert [(d.port, d.name) for d in radios] == [("/dev/spidev1.0", None)]
    assert spiradio.board_name(SpiWiring()) == ""


def test_the_keyboard_s_names_are_part_of_its_mark(uconsole: set) -> None:
    """1eaf:0024 is any LeafLabs Maple. Only the uConsole keyboard of ClockworkPi counts."""
    uconsole.discard(spiradio.UCONSOLE_KEYBOARD)
    uconsole.add(("1eaf", "0024", "LeafLabs", "Maple"))
    assert spiradio.board_name(SpiWiring()) == ""


def test_usb_devices_are_read_from_sysfs(tmp_path: Path) -> None:
    """The ids, the maker, and the product of each device. A device with no name reads as empty."""
    keyboard = tmp_path / "1-1.1"
    keyboard.mkdir()
    for name, value in (
        ("idVendor", "1eaf"),
        ("idProduct", "0024"),
        ("manufacturer", "ClockworkPI"),
        ("product", "uConsole"),
    ):
        (keyboard / name).write_text(value + "\n", encoding="utf-8")
    hub = tmp_path / "1-1.4"
    hub.mkdir()
    (hub / "idVendor").write_text("1a86\n", encoding="utf-8")
    (hub / "idProduct").write_text("8091\n", encoding="utf-8")
    (tmp_path / "1-1.1-if0").mkdir()  # an interface, not a device: it has no idVendor
    assert spiradio.usb_devices(tmp_path) == {
        ("1eaf", "0024", "ClockworkPI", "uConsole"),
        ("1a86", "8091", "", ""),
    }
    assert spiradio.usb_devices(tmp_path / "nowhere") == frozenset()


def test_the_device_tree_is_read_whole(tmp_path: Path) -> None:
    """MeshTerm reads each compatible at any depth, and each NUL-separated string of a node."""
    panel = tmp_path / "axi" / "dsi@128000" / "panel@0"
    panel.mkdir(parents=True)
    (panel / "compatible").write_bytes(b"cw,cwu50\0")
    (tmp_path / "compatible").write_bytes(b"raspberrypi,5-compute-module\0brcm,bcm2712\0")
    assert spiradio.device_tree_compatibles(tmp_path) == {
        "cw,cwu50",
        "raspberrypi,5-compute-module",
        "brcm,bcm2712",
    }
    assert spiradio.device_tree_compatibles(tmp_path / "nowhere") == frozenset()


def test_a_profile_on_the_cap_s_node_wins(cardputer: Path) -> None:
    """A profile on the same node is the word of the owner, and it replaces the shipped one."""
    mine = replace(spiradio.CARDPUTER_ZERO_CAP, use_dio3_tcxo=False)
    profiles = {"cap": DeviceProfile(name="cap", transport="spi", spi=mine)}
    radios = spiradio.spi_radios(profiles)
    assert [(d.name, d.spi) for d in radios] == [("cap", mine)]


def test_the_cap_is_what_spi_and_a_remembered_node_reach(contexts, cardputer: Path) -> None:  # noqa: ANN001
    """``--spi``, and a radio that MeshTerm remembers by its node, give the wiring of the Cap."""
    assert contexts(spi_override=True).resolve_spi() == spiradio.CARDPUTER_ZERO_CAP
    assert contexts().spi_wiring_for("/dev/spidev0.1") == spiradio.CARDPUTER_ZERO_CAP


def test_the_cardputer_cap_wiring_is_the_one_the_hardware_answered_on() -> None:
    """The lines that the M5 factory test drives, on the node that the panel shares.

    The power comes from the LED.
    """
    cap = spiradio.CARDPUTER_ZERO_CAP
    assert cap.spidev == "/dev/spidev0.1"
    assert (cap.reset_pin, cap.busy_pin, cap.irq_pin) == (26, 22, 23)
    assert cap.leds == ("ext_5v_out=1", "ext_usb_gpio_fun=0")
    assert (cap.pi4io_bus, cap.pi4io_address, cap.pi4io_high) == (1, 0x43, (0,))
    assert cap.use_dio2_rf and cap.use_dio3_tcxo
    # Its GPS, read at 115200 on the UART of the header while the Cap had power.
    assert (cap.gps_port, cap.gps_baud) == ("/dev/serial0", 115200)


def test_a_board_s_gps_comes_from_the_table() -> None:
    """A GPS next to the radio is also wiring: a port and the speed of its communication."""
    wiring = SpiWiring.from_toml({"gps_port": "/dev/ttyAMA0", "gps_baud": 38400})
    assert (wiring.gps_port, wiring.gps_baud) == ("/dev/ttyAMA0", 38400)
    assert (SpiWiring().gps_port, SpiWiring().gps_baud) == ("", 9600)  # no port, NMEA speed
    with pytest.raises(ValueError, match="gps_baud = 'fast' is not a int"):
        SpiWiring.from_toml({"gps_baud": "fast"})


@pytest.fixture()
def serial0(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/dev/serial0`` as Raspberry Pi OS makes it: a link to the UART that the scan reports."""
    links = {"/dev/serial0": "/dev/ttyS0"}
    monkeypatch.setattr(spiradio.os.path, "realpath", lambda p: links.get(p, p))


#: The GPS of the Cap as pyserial reports it: a header UART with no USB identity, so "n/a".
_GPS_PORT = DiscoveredDevice(port="/dev/ttyS0", description="n/a", hwid="n/a")
_USB_BOARD = DiscoveredDevice(port="/dev/ttyACM0", description="T1000-E", vid=0x239A)


def test_the_cap_s_gps_is_part_of_the_radio_not_a_companion(cardputer: Path, serial0) -> None:  # noqa: ANN001
    """MeshTerm removes the port of the Cap GPS from each list that shows the Cap."""
    assert spiradio.without_radio_ports([_GPS_PORT, _USB_BOARD], {}) == [_USB_BOARD]


def test_the_same_port_on_another_pi_is_left_listed(cardputer: Path, serial0) -> None:  # noqa: ANN001
    """With no Cardputer, there is no Cap. The user can choose what answers on that UART."""
    cardputer.rmdir()
    assert spiradio.without_radio_ports([_GPS_PORT, _USB_BOARD], {}) == [_GPS_PORT, _USB_BOARD]


def test_a_profile_s_gps_port_is_its_radio_s(cardputer: Path, serial0) -> None:  # noqa: ANN001
    """A profile that names its own GPS claims that port. MeshTerm does not assume the Cap port."""
    mine = replace(spiradio.CARDPUTER_ZERO_CAP, gps_port="/dev/ttyACM0")
    profiles = {"cap": DeviceProfile(name="cap", transport="spi", spi=mine)}
    assert spiradio.without_radio_ports([_GPS_PORT, _USB_BOARD], profiles) == [_GPS_PORT]


# --- finding a Python for the node ---------------------------------------------------------


def test_a_named_python_is_the_only_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named interpreter is a decision. A fallback past it would hide that it fails."""
    probed: list[str] = []
    monkeypatch.setattr(spiradio, "_can_import_runtime", lambda p: probed.append(p) or False)
    with pytest.raises(spiradio.SpiRadioError) as err:
        spiradio.find_runtime(SpiWiring(python="/opt/radio/bin/python"))
    assert err.value.kind == "runtime"
    assert probed == ["/opt/radio/bin/python"]


def test_the_first_python_with_the_library_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The search tries candidates in order, and stops at the first that can import the library."""
    monkeypatch.setattr(spiradio, "_runtime_candidates", lambda w: ["/a", "/b", "/c"])
    monkeypatch.setattr(spiradio, "_can_import_runtime", lambda p: p != "/a")
    assert spiradio.find_runtime(SpiWiring()) == "/b"


def test_a_frozen_build_hands_the_node_a_clean_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another Python must load its own libraries, not the unpacked copies of the one-file build."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/tmp/_MEI1")
    monkeypatch.setenv("_MEIPASS2", "/tmp/_MEI1")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI1")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    env = spiradio.child_env()
    assert "_MEIPASS2" not in env and not any(k.startswith("_PYI") for k in env)
    assert env["LD_LIBRARY_PATH"] == "/usr/local/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in env
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    assert "LD_LIBRARY_PATH" not in spiradio.child_env()


# --- first use: the node of the bridge carries over ----------------------------------------


def _bridge_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An XDG data home with the state of a bridge, and a stub for the radio settings."""
    data = tmp_path / "share"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(spiradio, "_bridge_radio_seed", lambda: {"frequency_hz": 869_525_000})
    bridge = data / "meshterm-spi-bridge"
    bridge.mkdir(parents=True)
    (bridge / "identity.key").write_bytes(b"\xbb" * 32)
    (bridge / "contacts.json").write_text('[{"public_key": "ab", "name": "Ridge"}]')
    return data


def test_the_bridge_node_carries_over_the_first_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity, contacts, name, and radio carry over: it is the node that users know."""
    _bridge_home(tmp_path, monkeypatch)
    state = tmp_path / "radio" / "spidev1.0"
    carried = spiradio.import_bridge_state(state)
    assert len(carried) == 3
    assert (state / "identity.key").read_bytes() == b"\xbb" * 32
    assert json.loads((state / "contacts.json").read_text())[0]["name"] == "Ridge"
    prefs = json.loads((state / "prefs.json").read_text())
    assert prefs == {"node_name": "uConsole", "frequency_hz": 869_525_000}
    assert spiradio.import_bridge_state(state) == [], "never twice: the node has its own now"


def test_the_gui_key_wins_over_the_bridge_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge shares the key of the meshcore-uconsole GUI when there is one, so use it."""
    data = _bridge_home(tmp_path, monkeypatch)
    (data / "meshcore-uconsole").mkdir()
    (data / "meshcore-uconsole" / "identity.key").write_bytes(b"\xcc" * 32)
    state = tmp_path / "radio" / "spidev1.0"
    spiradio.import_bridge_state(state)
    assert (state / "identity.key").read_bytes() == b"\xcc" * 32


def test_no_bridge_means_nothing_carried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no key to inherit, the node makes its own key, and MeshTerm copies nothing else."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty"))
    state = tmp_path / "radio" / "spidev1.0"
    assert spiradio.import_bridge_state(state) == []
    assert not state.exists()


# --- the error that a user reads -----------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "chip"),
    [
        ({0: "pinctrl-bcm2711", 1: "raspberrypi-exp-gpio"}, 0),  # a CM4
        ({0: "gpio-brcmstb@107d508500", 4: "pinctrl-rp1"}, 4),  # a CM5 on its first kernels
        ({0: "pinctrl-rp1", 10: "gpio-brcmstb@107d508500"}, 0),  # a CM5 after the alias
        ({0: "gpio-brcmstb@107d508500", 15: "pinctrl-rp1"}, 15),  # a CM5 whose chip moved
        ({0: "gpio-mockup-A"}, None),  # no Raspberry Pi header
    ],
)
def test_the_header_s_gpio_chip_is_found_by_its_label(labels: dict, chip: int | None) -> None:
    """The label gives the controller of the header, whatever number this kernel gave it.

    A CM4 and a CM5 put their 40-pin header on chips with different numbers. On the CM5, the
    number changed between kernel releases. Thus the defaults of the AIO find the chip by name.
    """
    assert spiradio.header_gpio_chip(labels) == chip


def test_a_profile_s_gpio_chip_pins_it_and_the_default_looks(monkeypatch) -> None:
    """A number in the profile wins. If there is none, the label decides. Chip 0 is the fallback."""
    monkeypatch.setattr(spiradio, "gpio_chip_labels", lambda: {15: "pinctrl-rp1"})
    assert SpiWiring().gpio_chip == -1
    assert spiradio.gpio_chip(SpiWiring()) == 15
    assert spiradio.gpio_chip(SpiWiring(gpio_chip=4)) == 4
    assert "sudo lsof /dev/gpiochip15" in spiradio.explain("busy", "", SpiWiring())
    monkeypatch.setattr(spiradio, "gpio_chip_labels", lambda: {})
    assert spiradio.gpio_chip(SpiWiring()) == 0


def test_reading_the_chips_labels_never_fails(tmp_path: Path) -> None:
    """Off Linux, or where MeshTerm cannot ask a chip, there is nothing to report."""
    (tmp_path / "gpiochip0").write_bytes(b"")  # a plain file: the ioctl cannot answer it
    assert spiradio.gpio_chip_labels(tmp_path) == {}


@pytest.mark.parametrize(
    ("kind", "holder", "says"),
    [
        ("busy", "bridge", "systemctl --user stop meshterm-spi-bridge"),
        ("busy", "console", "meshcore-console has the radio open"),
        ("busy", None, "sudo lsof /dev/gpiochip0"),
        ("runtime", None, "pipx inject mesh-term 'openhop-core[hardware]'"),
        ("no-spi", None, "dtoverlay=spi1-1cs"),
        ("permission", None, "sudo usermod -aG spi,gpio $USER"),
    ],
)
def test_every_failure_says_what_to_do(kind: str, holder: str | None, says: str) -> None:
    """Each way that the node can fail to start comes with the command that corrects it."""
    assert says in spiradio.explain(kind, "detail", SpiWiring(), holder)


# --- the node process ----------------------------------------------------------------------

_READY = """
import json, sys
print(json.dumps({"event": "ready", "port": 4321, "public_key": "ab" * 32}), flush=True)
sys.stdin.buffer.read()
"""

_REFUSES = """
import json
message = "no access to /dev/spidev1.0"
print(json.dumps({"event": "error", "kind": "permission", "message": message}))
"""

_SILENT = "import sys\nsys.exit(3)\n"

_DEAF = """
import json, time
print(json.dumps({"event": "ready", "port": 4321}), flush=True)
while True:
    time.sleep(1)
"""


def _node(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> None:
    """Use ``source`` as the node script, and run it with this interpreter on "Linux"."""
    script = tmp_path / "node.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.setattr(spiradio, "node_script", lambda: script)
    monkeypatch.setattr(spiradio, "_on_linux", lambda: True)
    monkeypatch.setattr(spiradio, "radio_holder", lambda: None)
    monkeypatch.setattr(spiradio, "find_runtime", lambda wiring: sys.executable)
    monkeypatch.setattr(spiradio, "_bridge_radio_seed", dict)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "no-bridge"))


@pytest.mark.asyncio
async def test_a_ready_node_reports_its_port_and_leaves_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one line of the node is its port. To end the node, close its stdin."""
    _node(monkeypatch, tmp_path, _READY)
    node = await spiradio.start_node(SpiWiring(), tmp_path / "state", node_name="Test")
    assert (node.port, node.public_key) == (4321, "ab" * 32) and node.alive
    await node.stop()
    assert not node.alive and node.process.returncode == 0


@pytest.mark.asyncio
async def test_a_refusal_arrives_as_its_kind_with_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error line of the node becomes an error with an action, not "didn't answer"."""
    _node(monkeypatch, tmp_path, _REFUSES)
    with pytest.raises(spiradio.SpiRadioError) as err:
        await spiradio.start_node(SpiWiring(), tmp_path / "state", node_name="Test")
    assert err.value.kind == "permission"
    assert "usermod -aG spi,gpio" in str(err.value)


@pytest.mark.asyncio
async def test_a_node_that_dies_silently_points_at_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the node says nothing, the log is the only place for the reason."""
    _node(monkeypatch, tmp_path, _SILENT)
    with pytest.raises(spiradio.SpiRadioError) as err:
        await spiradio.start_node(SpiWiring(), tmp_path / "state", node_name="Test")
    assert err.value.kind == "failed" and "node.log" in str(err.value)


@pytest.mark.asyncio
async def test_a_node_deaf_to_its_stdin_is_ended_anyway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged node never holds the radio after a quit, because the signals end it."""
    _node(monkeypatch, tmp_path, _DEAF)
    monkeypatch.setattr(spiradio, "STOP_GRACE_S", 0.3)
    node = await spiradio.start_node(SpiWiring(), tmp_path / "state", node_name="Test")
    await node.stop()
    assert not node.alive


@pytest.mark.asyncio
async def test_a_known_holder_is_named_before_any_node_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the bridge service holds the radio, the error says so at once, and says how to stop it."""
    _node(monkeypatch, tmp_path, _READY)
    monkeypatch.setattr(spiradio, "radio_holder", lambda: "bridge")
    with pytest.raises(spiradio.SpiRadioError, match="systemctl --user stop"):
        await spiradio.start_node(SpiWiring(), tmp_path / "state", node_name="Test")


@pytest.mark.asyncio
async def test_only_linux_drives_an_spi_radio(tmp_path: Path) -> None:
    """On other platforms, the radio cannot be there, and the reason is the platform."""
    if sys.platform.startswith("linux"):
        pytest.skip("this is Linux")
    with pytest.raises(spiradio.SpiRadioError) as err:
        await spiradio.start_node(SpiWiring(), tmp_path, node_name="Test")
    assert err.value.kind == "unsupported"


@pytest.mark.asyncio
async def test_the_device_ends_its_node_when_the_connection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node whose companion never answers must not keep the radio."""
    _node(monkeypatch, tmp_path, _READY)
    started: list[spiradio.NodeProcess] = []
    real_start = spiradio.start_node

    async def start(*args: object, **kwargs: object) -> spiradio.NodeProcess:
        node = await real_start(*args, **kwargs)  # type: ignore[arg-type]
        started.append(node)
        return node

    async def no_companion(self: object) -> None:
        raise spiradio.DeviceCommandError("no answer")

    monkeypatch.setattr(spiradio, "start_node", start)
    monkeypatch.setattr(spiradio.MeshCoreDevice, "connect", no_companion)
    device = spiradio.SpiRadioDevice(SpiWiring(), tmp_path / "state")
    assert device.transport == "spi" and device.endpoint == "/dev/spidev1.0"
    with pytest.raises(spiradio.DeviceCommandError):
        await device.connect()
    assert started and not started[0].alive


# --- where it shows up ---------------------------------------------------------------------


def test_each_radio_whose_device_node_exists_is_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A radio of a profile has the profile name. The AIO node is alone if no profile covers it."""
    present = {"/dev/spidev1.0", "/dev/spidev0.0"}
    monkeypatch.setattr(spiradio, "spi_present", lambda w: w.spidev in present)
    hat = SpiWiring(bus_id=0, reset_pin=17)
    profiles = {
        "hat": DeviceProfile(name="hat", transport="spi", spi=hat),
        "gone": DeviceProfile(name="gone", transport="spi", spi=SpiWiring(bus_id=3)),
    }
    rows = spiradio.spi_radios(profiles)
    assert [(r.port, r.name) for r in rows] == [("/dev/spidev0.0", "hat"), ("/dev/spidev1.0", None)]
    # A profile on the node of the AIO takes that row, instead of a row with no name.
    profiles["aio"] = DeviceProfile(name="aio", transport="spi")
    rows = spiradio.spi_radios(profiles)
    assert [r.name for r in rows] == ["hat", "aio"]
    # Also, MeshTerm does not list a radio two times when it is already in the list.
    assert spiradio.spi_radios(profiles, rows) == []


def test_spi_and_another_radio_on_one_command_line_is_a_usage_error() -> None:
    """The SPI radio is on this machine, and each other flag names a radio that is not."""
    from typer.testing import CliRunner

    from meshterm.cli import app

    result = CliRunner().invoke(app, ["--spi", "--port", "COM7", "info"])
    assert result.exit_code == 2
    assert "--spi and --port name different devices" in result.output


def test_meshterm_devices_lists_the_spi_radio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI inventory shows the radio that the device screen shows, active under --spi."""
    from typer.testing import CliRunner

    from meshterm.cli import app
    from meshterm.tools import devices

    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path))
    monkeypatch.setattr(devices, "discover_devices", list)
    monkeypatch.setattr(spiradio, "spi_present", lambda w: w.spidev == "/dev/spidev1.0")
    result = CliRunner().invoke(app, ["--spi", "--json", "devices", "--no-ble"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [(r["target"], r["transport"], r["active"]) for r in rows] == [
        ("/dev/spidev1.0", "spi", True)
    ]
