# SPDX-License-Identifier: Apache-2.0
"""Tests for the console-font switch of the PicoCalc: the gate, the three verbs, and the wiring.

No test here runs ``setfont``. The module under test starts another program, so the tests
check which command line the module builds. They also check that it never builds a command
line on a machine that it must not change. A recorder replaces :func:`subprocess.run` in
all the tests. A real call would make a test change the console of the developer.

The tests ask three questions, in this order:

1. Does :func:`~meshterm.services.consolefont.applies` return false in each case where it
   must?
2. Do the verbs give the correct arguments to ``setfont``, and do they ignore a failure?
3. Is the switch connected to the menu only? Then a scripted command that the user types
   into the terminal of another person never repaints it.
"""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.core.preferences import Preferences, by_group, get_spec
from meshterm.persistence.repository import Repository
from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.services import consolefont
from meshterm.tools.preferences import PreferencesTool
from meshterm.ui.theme import active_theme

#: The result that :func:`subprocess.run` returns for a ``setfont`` that succeeded.
_OK = subprocess.CompletedProcess(["setfont"], 0, "", "")


class _Recorder:
    """A substitute for :func:`subprocess.run`.

    It stores each argv and gives the scripted answers.
    """

    def __init__(self, answers: list[subprocess.CompletedProcess] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = list(answers or [])

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        return self.answers.pop(0) if self.answers else _OK


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Make the process look like MeshTerm that runs on the own VT of the handheld."""
    set_platform(PICOCALC_LYRA)
    monkeypatch.setattr(os, "ttyname", lambda fd: "/dev/tty1", raising=False)
    monkeypatch.setattr(consolefont.shutil, "which", lambda name: "/usr/bin/setfont")
    recorder = _Recorder()
    monkeypatch.setattr(consolefont.subprocess, "run", recorder)
    return recorder


# -- the gate --------------------------------------------------------------------


def test_the_switch_applies_on_the_handhelds_own_console(device: _Recorder) -> None:
    """The switch applies when all three conditions are true.

    The conditions are: the platform is the PicoCalc, stdin is a Linux VT, and a ``setfont``
    is available to run.
    """
    assert consolefont.applies() is True


def test_a_desktop_never_touches_a_console_font(device: _Recorder) -> None:
    """A desktop never changes a console font.

    The fonts belong to the PicoCalc. No other platform has them or needs them.
    """
    set_platform(REGULAR)
    assert consolefont.applies() is False


@pytest.mark.parametrize("tty", ["/dev/pts/3", "/dev/ttyS1", "/dev/console"])
def test_anything_that_is_not_a_linux_vt_is_left_alone(
    device: _Recorder, monkeypatch: pytest.MonkeyPatch, tty: str
) -> None:
    """The switch leaves alone each tty that is not a Linux VT.

    An ssh pty has no font of its own. A serial line is not a console that MeshTerm owns.
    """
    monkeypatch.setattr(os, "ttyname", lambda fd: tty, raising=False)
    assert consolefont.applies() is False


def test_a_redirected_stdin_is_not_a_vt(device: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirected stdin is not a VT.

    If ``os.ttyname`` raises an exception (for a pipe, or on Windows, which has no such
    call), the answer is no.
    """

    def _no_tty(fd: int) -> str:
        raise OSError(25, "not a tty")

    monkeypatch.setattr(os, "ttyname", _no_tty, raising=False)
    assert consolefont.applies() is False


def test_no_setfont_means_no_switching(device: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """If there is no ``setfont``, there is no switch.

    The font build also needs the kbd tools. Without them, no verb works.
    """
    monkeypatch.setattr(consolefont.shutil, "which", lambda name: None)
    assert consolefont.applies() is False


def test_a_gated_out_machine_runs_nothing_at_all(device: _Recorder) -> None:
    """A machine that the gate excludes runs nothing.

    Each verb asks the gate first. Thus a no does nothing, and it does not start a
    subprocess by mistake.
    """
    set_platform(REGULAR)
    assert consolefont.remember() is None
    assert consolefont.apply("6x8") is False
    assert device.calls == []


# -- the three verbs -------------------------------------------------------------


def test_remember_saves_the_font_the_shell_had(device: _Recorder) -> None:
    """``setfont -O`` writes the current font and its unicode table.

    The file goes in the home directory of the app.
    """
    saved = consolefont.remember()
    assert saved is not None
    assert saved.name == consolefont.SAVED_NAME
    assert device.calls == [["setfont", "-O", str(saved)]]


def test_apply_loads_the_file_the_preference_names(device: _Recorder) -> None:
    """Apply loads the file that the preference names.

    The value is a cell size. This module owns the path that the value maps to. The path is
    not a preference.
    """
    assert consolefont.apply("6x8") is True
    assert device.calls == [["setfont", "/usr/share/consolefonts/meshterm8.psf.gz"]]
    assert consolefont.apply("6x12") is True
    assert device.calls[-1] == ["setfont", "/usr/share/consolefonts/meshterm.psf.gz"]


def test_no_call_ever_names_a_tty(device: _Recorder) -> None:
    """No call names a tty. It never uses ``-C``.

    ``setfont`` with no tty name acts on the console that the user looks at.
    """
    consolefont.remember()
    consolefont.apply("6x8")
    assert not any("-C" in argv for argv in device.calls)


def test_a_value_with_no_font_behind_it_changes_nothing(device: _Recorder) -> None:
    """A value that names no font changes nothing.

    A key from a file that the user edited by hand can name no font. The result is that
    nothing happens, and there is no crash.
    """
    assert consolefont.apply("8x16") is False
    assert device.calls == []


def test_restore_reloads_the_saved_font_and_drops_the_file(
    device: _Recorder, tmp_path: Path
) -> None:
    """The way out: load again exactly the font that the app saved, then leave no file."""
    saved = tmp_path / consolefont.SAVED_NAME
    saved.write_bytes(b"PSF")
    consolefont.restore(saved)
    assert device.calls == [["setfont", str(saved)]]
    assert not saved.exists()


def test_restoring_twice_is_harmless(device: _Recorder, tmp_path: Path) -> None:
    """A second restore does no harm.

    The ``finally`` of the session and the atexit hook both run. The second one finds
    nothing to do.
    """
    saved = tmp_path / consolefont.SAVED_NAME
    saved.write_bytes(b"PSF")
    consolefont.restore(saved)
    consolefont.restore(saved)
    consolefont.restore(None)
    assert device.calls == [["setfont", str(saved)]]


def test_a_setfont_that_fails_is_swallowed(
    device: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``setfont`` that fails is ignored.

    A font is only cosmetic. A non-zero exit status gives a log line and never an
    exception.
    """
    monkeypatch.setattr(
        consolefont.subprocess,
        "run",
        _Recorder([subprocess.CompletedProcess(["setfont"], 1, "", "setfont: KDFONTOP: EINVAL")]),
    )
    assert consolefont.apply("6x8") is False


def test_a_setfont_that_cannot_be_run_is_swallowed(
    device: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``setfont`` that cannot run is ignored.

    The same rule applies to a binary that disappears between the ``which()`` and the exec,
    and to a timeout.
    """

    def _boom(argv: list[str], **kwargs: Any) -> Any:
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(consolefont.subprocess, "run", _boom)
    assert consolefont.apply("6x8") is False
    assert consolefont.remember() is None


def test_a_failed_remember_leaves_nothing_to_restore(
    device: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``remember`` that fails leaves nothing to restore.

    If nothing is saved, there is nothing to load again. The ``finally`` of the caller gets
    a None.
    """
    monkeypatch.setattr(
        consolefont.subprocess,
        "run",
        _Recorder([subprocess.CompletedProcess(["setfont"], 1, "", "denied")]),
    )
    assert consolefont.remember() is None


# -- the preference --------------------------------------------------------------


def test_the_preference_is_offered_on_the_handheld_only() -> None:
    """The preference is offered on the handheld only.

    A row about hardware that the desktop does not have is a question that the desktop
    cannot answer.
    """
    spec = get_spec("console_font")
    assert spec.offered_on("picocalc-lyra") is True
    assert spec.offered_on("regular") is False
    # All other preferences are offered on all platforms. The gate is the exception.
    gated = {s.key for _, specs in by_group() for s in specs if s.platforms is not None}
    assert gated == {"console_font"}


@pytest.mark.parametrize("platform", [REGULAR, PICOCALC_LYRA])
def test_the_preference_round_trips_on_every_platform(tmp_path: Path, platform: Any) -> None:
    """The preference makes a round trip on each platform.

    A file that was written on the handheld loads, keeps its value, and saves again on a
    desktop.
    """
    set_platform(platform)
    path = tmp_path / "preferences.toml"
    written = Preferences(path)
    written.set("console_font", "6x8")
    written.save()

    reread = Preferences.load(path)
    assert reread.get("console_font") == "6x8"
    # A second save keeps it. The gate hides a row, and it never removes a value.
    reread.save()
    assert "console_font" in path.read_text(encoding="utf-8")
    assert Preferences.load(path).get("console_font") == "6x8"


def test_the_page_draws_the_row_on_the_handheld_and_not_on_the_desktop() -> None:
    """The page draws the row on the handheld and not on the desktop.

    This is the one visible effect of the gate: a Display row that exists on one platform.
    """
    from meshterm.ui.preferences import _menu_items

    set_platform(PICOCALC_LYRA)
    _, items = _menu_items(Preferences(), {})
    assert "console_font" in [getattr(item, "value", None) for item in items]

    set_platform(REGULAR)
    _, items = _menu_items(Preferences(), {})
    assert "console_font" not in [getattr(item, "value", None) for item in items]


def test_the_printed_table_still_names_every_preference() -> None:
    """The printed table still names each preference.

    The listing of the CLI does not depend on the platform. This is on purpose, because
    nobody could set a key that the listing hides.
    """
    from meshterm.ui.preferences import preferences_table

    set_platform(REGULAR)
    console = Console(width=120, file=io.StringIO(), theme=active_theme(), legacy_windows=False)
    with console.capture() as capture:
        console.print(preferences_table(Preferences(), 120))
    assert "console_font" in capture.get()


# -- the wiring ------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path: Path) -> Any:
    """A context that uses the mock device. Its preferences are in a temporary file."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "prefs.db")
    context = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


async def test_the_command_line_never_repaints_the_console(
    device: _Recorder, ctx: AppContext
) -> None:
    """`meshterm preferences set console_font 6x8` writes the file and changes nothing else.

    The user types a scripted run into a console that MeshTerm does not own. MeshTerm is
    there as a guest. In half of the cases, it is an ssh session onto the handheld from
    another place.
    """
    result = await PreferencesTool().run(ctx, {"ops": [("set", "console_font", "6x8")]})
    assert result.summary == {"changes": 1}
    assert ctx.preferences.get("console_font") == "6x8"
    assert device.calls == []


async def test_the_page_switches_the_font_as_it_saves(device: _Recorder, ctx: AppContext) -> None:
    """The page switches the font when it saves.

    On the menu path, the app loads the new font at once, and not at the next start.
    """
    from meshterm.ui.surface import TuiUi

    ctx.ui = TuiUi(object())  # type: ignore[arg-type]
    await PreferencesTool().run(ctx, {"ops": [("set", "console_font", "6x8")]})
    assert device.calls == [["setfont", "/usr/share/consolefonts/meshterm8.psf.gz"]]


async def test_another_preference_leaves_the_font_alone(device: _Recorder, ctx: AppContext) -> None:
    """Another preference leaves the font alone.

    Only the one preference has an effect outside the app. The app only writes the others.
    """
    from meshterm.ui.surface import TuiUi

    ctx.ui = TuiUi(object())  # type: ignore[arg-type]
    await PreferencesTool().run(ctx, {"ops": [("set", "history_days", "30")]})
    assert device.calls == []
