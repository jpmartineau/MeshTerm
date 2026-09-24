# SPDX-License-Identifier: Apache-2.0
"""MeshTerm's half of the SPI radio: wiring, finding the node's Python, and its lifetime.

The node itself is :mod:`tests.test_radionode`. Here a stand-in node script plays its part —
one status line on stdout, then wait for stdin to close — so the protocol between the two
processes, the error wording and the shutdown ladder are exercised on any platform.
"""

from __future__ import annotations

import io
import json
import sys
import textwrap
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core import spiradio
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import DeviceProfile, Settings, SpiWiring
from meshterm.core.device_store import DeviceStore
from meshterm.core.discovery import TRANSPORT_SPI, spi_device
from meshterm.persistence.repository import Repository

# --- wiring --------------------------------------------------------------------------------


def test_the_default_wiring_is_the_uconsole_aio() -> None:
    """A profile with no ``spi`` table is the AIO v1, which is what the defaults describe."""
    wiring = SpiWiring()
    assert (wiring.bus_id, wiring.reset_pin, wiring.busy_pin, wiring.irq_pin) == (1, 25, 24, 26)
    assert wiring.use_dio2_rf and wiring.use_dio3_tcxo and wiring.en_pins == ()
    assert wiring.spidev == "/dev/spidev1.0"


def test_a_wiring_table_states_only_what_differs() -> None:
    """The AIO v2's one difference is its power-enable pin; nothing else moves."""
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
    """A misspelt pin would otherwise leave a default in force and a deaf radio."""
    with pytest.raises(ValueError, match=complaint.replace("(", r"\(").replace(")", r"\)")):
        SpiWiring.from_toml(table)


def test_the_retired_preamble_key_is_refused_with_its_reason() -> None:
    """A config still carrying the old fixed preamble is told why, not left deaf."""
    with pytest.raises(ValueError, match="preamble_length is no longer a wiring key"):
        SpiWiring.from_toml({"preamble_length": 12})


def test_a_profile_with_an_spi_table_is_an_spi_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``spi`` table implies the transport, the way ``host`` implies TCP."""
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
    """A wiring error is reported against the profile it is in, not as a bare key."""
    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[profiles.aio.spi]\nirq = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[profiles\.aio\.spi\]"):
        Settings.load()


def test_state_lives_under_the_spi_device_not_the_profile(tmp_path: Path) -> None:
    """One radio is one node however it was reached, so its key is filed by device node."""
    assert spiradio.state_dir(tmp_path, SpiWiring()) == tmp_path / "radio" / "spidev1.0"
    assert spiradio.state_dir(tmp_path, SpiWiring(bus_id=0, cs_id=1)).name == "spidev0.1"


def test_an_spi_radio_lists_like_a_device_on_its_node() -> None:
    """The picker row is named by the device node, which is also its remembered identity."""
    device = spi_device(SpiWiring(), name="aio")
    assert device.transport == TRANSPORT_SPI and device.is_spi
    assert device.target == "/dev/spidev1.0"
    assert device.stable_id == "spi:/dev/spidev1.0"
    assert device.label == "aio (/dev/spidev1.0)"
    assert spi_device(SpiWiring()).label == "SPI radio (/dev/spidev1.0)"


# --- choosing the radio --------------------------------------------------------------------


@pytest.fixture()
def contexts(tmp_path: Path):  # noqa: ANN201 - a factory fixture
    """Build contexts over a scratch config directory, closing their databases after."""
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
    """``--spi`` means the radio on this machine, wired as its profile says when there is one."""
    wired = SpiWiring(en_pins=(27,))
    profiles = {"aio2": DeviceProfile(name="aio2", transport="spi", spi=wired)}
    assert contexts(spi_override=True, profiles=profiles).resolve_spi() == wired
    assert contexts(spi_override=True).resolve_spi() == SpiWiring()
    assert contexts(spi_override=True).active_transport == "spi"


def test_another_named_radio_is_not_the_spi_radio(contexts) -> None:  # noqa: ANN001
    """A port, an address or a host named outright means the session is about that radio."""
    for override in (
        {"port_override": "COM7"},
        {"tcp_override": "10.0.0.2"},
        {"ble_override": "AA"},
    ):
        assert contexts(**override).resolve_spi() is None


def test_a_listed_radio_is_wired_by_the_profile_on_its_node(contexts) -> None:  # noqa: ANN001
    """A remembered or listed radio is known by its node, and its pins come from that."""
    other = SpiWiring(bus_id=0, reset_pin=17)
    profiles = {"hat": DeviceProfile(name="hat", transport="spi", spi=other)}
    ctx = contexts(profiles=profiles)
    assert ctx.spi_wiring_for("/dev/spidev0.0") == other
    assert ctx.spi_wiring_for("/dev/spidev1.0") == SpiWiring()


# --- finding a Python for the node ---------------------------------------------------------


def test_a_named_python_is_the_only_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming an interpreter is a decision; falling back past it would hide that it fails."""
    probed: list[str] = []
    monkeypatch.setattr(spiradio, "_can_import_runtime", lambda p: probed.append(p) or False)
    with pytest.raises(spiradio.SpiRadioError) as err:
        spiradio.find_runtime(SpiWiring(python="/opt/radio/bin/python"))
    assert err.value.kind == "runtime"
    assert probed == ["/opt/radio/bin/python"]


def test_the_first_python_with_the_library_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Candidates are tried in order, and the search stops at the first that can import it."""
    monkeypatch.setattr(spiradio, "_runtime_candidates", lambda w: ["/a", "/b", "/c"])
    monkeypatch.setattr(spiradio, "_can_import_runtime", lambda p: p != "/a")
    assert spiradio.find_runtime(SpiWiring()) == "/b"


def test_a_frozen_build_hands_the_node_a_clean_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another Python must load its own libraries, not the one-file build's unpacked copies."""
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


# --- first use: the bridge's node carries over ---------------------------------------------


def _bridge_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An XDG data home holding a bridge's state, with the service's radio settings stubbed."""
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
    """Identity, contacts, name and radio: the node is the one everybody already knows."""
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
    """The bridge shares the meshcore-uconsole GUI's key when there is one, so that is it."""
    data = _bridge_home(tmp_path, monkeypatch)
    (data / "meshcore-uconsole").mkdir()
    (data / "meshcore-uconsole" / "identity.key").write_bytes(b"\xcc" * 32)
    state = tmp_path / "radio" / "spidev1.0"
    spiradio.import_bridge_state(state)
    assert (state / "identity.key").read_bytes() == b"\xcc" * 32


def test_no_bridge_means_nothing_carried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a key to inherit the node mints its own, and nothing else is copied either."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty"))
    state = tmp_path / "radio" / "spidev1.0"
    assert spiradio.import_bridge_state(state) == []
    assert not state.exists()


# --- the error a person reads --------------------------------------------------------------


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
    """Each way the node can fail to start comes with the command that fixes it."""
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
    """Stand ``source`` in for the node script, run by this interpreter on "Linux"."""
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
    """The node's one line is its port; closing its stdin is all it takes to end it."""
    _node(monkeypatch, tmp_path, _READY)
    node = await spiradio.start_node(SpiWiring(), tmp_path / "state", node_name="Test")
    assert (node.port, node.public_key) == (4321, "ab" * 32) and node.alive
    await node.stop()
    assert not node.alive and node.process.returncode == 0


@pytest.mark.asyncio
async def test_a_refusal_arrives_as_its_kind_with_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node's error line becomes an actionable error, not "didn't answer"."""
    _node(monkeypatch, tmp_path, _REFUSES)
    with pytest.raises(spiradio.SpiRadioError) as err:
        await spiradio.start_node(SpiWiring(), tmp_path / "state", node_name="Test")
    assert err.value.kind == "permission"
    assert "usermod -aG spi,gpio" in str(err.value)


@pytest.mark.asyncio
async def test_a_node_that_dies_silently_points_at_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing said, the log is the one place the reason can be."""
    _node(monkeypatch, tmp_path, _SILENT)
    with pytest.raises(spiradio.SpiRadioError) as err:
        await spiradio.start_node(SpiWiring(), tmp_path / "state", node_name="Test")
    assert err.value.kind == "failed" and "node.log" in str(err.value)


@pytest.mark.asyncio
async def test_a_node_deaf_to_its_stdin_is_ended_anyway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged node never holds the radio past a quit: the signals end it."""
    _node(monkeypatch, tmp_path, _DEAF)
    monkeypatch.setattr(spiradio, "STOP_GRACE_S", 0.3)
    node = await spiradio.start_node(SpiWiring(), tmp_path / "state", node_name="Test")
    await node.stop()
    assert not node.alive


@pytest.mark.asyncio
async def test_a_known_holder_is_named_before_any_node_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge service holding the radio is said at once, with how to stop it."""
    _node(monkeypatch, tmp_path, _READY)
    monkeypatch.setattr(spiradio, "radio_holder", lambda: "bridge")
    with pytest.raises(spiradio.SpiRadioError, match="systemctl --user stop"):
        await spiradio.start_node(SpiWiring(), tmp_path / "state", node_name="Test")


@pytest.mark.asyncio
async def test_only_linux_drives_an_spi_radio(tmp_path: Path) -> None:
    """Anywhere else the radio can't be there, and the reason is the platform."""
    if sys.platform.startswith("linux"):
        pytest.skip("this is Linux")
    with pytest.raises(spiradio.SpiRadioError) as err:
        await spiradio.start_node(SpiWiring(), tmp_path, node_name="Test")
    assert err.value.kind == "unsupported"


@pytest.mark.asyncio
async def test_the_device_ends_its_node_when_the_connection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node whose companion never answers must not be left holding the radio."""
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
    """A profile's radio by its name; the AIO's node on its own when no profile covers it."""
    present = {"/dev/spidev1.0", "/dev/spidev0.0"}
    monkeypatch.setattr(spiradio, "spi_present", lambda w: w.spidev in present)
    hat = SpiWiring(bus_id=0, reset_pin=17)
    profiles = {
        "hat": DeviceProfile(name="hat", transport="spi", spi=hat),
        "gone": DeviceProfile(name="gone", transport="spi", spi=SpiWiring(bus_id=3)),
    }
    rows = spiradio.spi_radios(profiles)
    assert [(r.port, r.name) for r in rows] == [("/dev/spidev0.0", "hat"), ("/dev/spidev1.0", None)]
    # A profile on the AIO's own node takes that row instead of a nameless one.
    profiles["aio"] = DeviceProfile(name="aio", transport="spi")
    rows = spiradio.spi_radios(profiles)
    assert [r.name for r in rows] == ["hat", "aio"]
    # And a radio already in the list is not listed twice.
    assert spiradio.spi_radios(profiles, rows) == []


def test_spi_and_another_radio_on_one_command_line_is_a_usage_error() -> None:
    """The SPI radio is on this machine, and every other flag names one that isn't."""
    from typer.testing import CliRunner

    from meshterm.cli import app

    result = CliRunner().invoke(app, ["--spi", "--port", "COM7", "info"])
    assert result.exit_code == 2
    assert "--spi and --port name different devices" in result.output


def test_meshterm_devices_lists_the_spi_radio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI inventory shows the radio the device screen shows, marked active under --spi."""
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
