# SPDX-License-Identifier: Apache-2.0
"""Tests for the PicoCalc's console-font switch: the gate, the three verbs, and the wiring.

Nothing here runs ``setfont`` — the point of the module under test is that it shells out,
so what is pinned is *which command line* it builds and that it never builds one on a
machine it has no business touching. :func:`subprocess.run` is replaced by a recorder
throughout; a real call would be a test changing the developer's console.

Three questions, in order: does :func:`~meshterm.services.consolefont.applies` say no
everywhere it should; do the verbs say the right thing to ``setfont`` and shrug off a
failure; and is the switch wired to the *menu* only, so a scripted command typed into
somebody else's terminal never repaints it.
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

#: What a successful ``setfont`` looks like coming back from :func:`subprocess.run`.
_OK = subprocess.CompletedProcess(["setfont"], 0, "", "")


class _Recorder:
    """Stands in for :func:`subprocess.run`, remembering every argv and answering to script."""

    def __init__(self, answers: list[subprocess.CompletedProcess] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = list(answers or [])

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        return self.answers.pop(0) if self.answers else _OK


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Make the process look like MeshTerm running on the handheld's own VT."""
    set_platform(PICOCALC_LYRA)
    monkeypatch.setattr(os, "ttyname", lambda fd: "/dev/tty1", raising=False)
    monkeypatch.setattr(consolefont.shutil, "which", lambda name: "/usr/bin/setfont")
    recorder = _Recorder()
    monkeypatch.setattr(consolefont.subprocess, "run", recorder)
    return recorder


# -- the gate --------------------------------------------------------------------


def test_the_switch_applies_on_the_handhelds_own_console(device: _Recorder) -> None:
    """Picocalc, a Linux VT on stdin, and a setfont to run: all three, so it applies."""
    assert consolefont.applies() is True


def test_a_desktop_never_touches_a_console_font(device: _Recorder) -> None:
    """The fonts are the PicoCalc's; no other platform has them or wants them."""
    set_platform(REGULAR)
    assert consolefont.applies() is False


@pytest.mark.parametrize("tty", ["/dev/pts/3", "/dev/ttyS1", "/dev/console"])
def test_anything_that_is_not_a_linux_vt_is_left_alone(
    device: _Recorder, monkeypatch: pytest.MonkeyPatch, tty: str
) -> None:
    """An ssh pty has no font of its own, and a serial line is not a console we own."""
    monkeypatch.setattr(os, "ttyname", lambda fd: tty, raising=False)
    assert consolefont.applies() is False


def test_a_redirected_stdin_is_not_a_vt(device: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.ttyname`` raising (a pipe, or Windows, which has no such call) means no."""

    def _no_tty(fd: int) -> str:
        raise OSError(25, "not a tty")

    monkeypatch.setattr(os, "ttyname", _no_tty, raising=False)
    assert consolefont.applies() is False


def test_no_setfont_means_no_switching(device: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kbd tools are what the font build needs too; without them there is no verb."""
    monkeypatch.setattr(consolefont.shutil, "which", lambda name: None)
    assert consolefont.applies() is False


def test_a_gated_out_machine_runs_nothing_at_all(device: _Recorder) -> None:
    """Every verb asks the gate first, so a no is a no-op and not a stray subprocess."""
    set_platform(REGULAR)
    assert consolefont.remember() is None
    assert consolefont.apply("6x8") is False
    assert device.calls == []


# -- the three verbs -------------------------------------------------------------


def test_remember_saves_the_font_the_shell_had(device: _Recorder) -> None:
    """``setfont -O`` writes the current font, unicode table and all, under the app's home."""
    saved = consolefont.remember()
    assert saved is not None
    assert saved.name == consolefont.SAVED_NAME
    assert device.calls == [["setfont", "-O", str(saved)]]


def test_apply_loads_the_file_the_preference_names(device: _Recorder) -> None:
    """The value is a cell size; the path it maps to is this module's business, not a preference."""
    assert consolefont.apply("6x8") is True
    assert device.calls == [["setfont", "/usr/share/consolefonts/meshterm8.psf.gz"]]
    assert consolefont.apply("6x12") is True
    assert device.calls[-1] == ["setfont", "/usr/share/consolefonts/meshterm.psf.gz"]


def test_no_call_ever_names_a_tty(device: _Recorder) -> None:
    """Never ``-C``: setfont with no tty named acts on the console the reader is looking at."""
    consolefont.remember()
    consolefont.apply("6x8")
    assert not any("-C" in argv for argv in device.calls)


def test_a_value_with_no_font_behind_it_changes_nothing(device: _Recorder) -> None:
    """A key from a hand-edited file that names no font is a no-op, not a crash."""
    assert consolefont.apply("8x16") is False
    assert device.calls == []


def test_restore_reloads_the_saved_font_and_drops_the_file(
    device: _Recorder, tmp_path: Path
) -> None:
    """The way out: put back exactly what was saved, then leave nothing behind."""
    saved = tmp_path / consolefont.SAVED_NAME
    saved.write_bytes(b"PSF")
    consolefont.restore(saved)
    assert device.calls == [["setfont", str(saved)]]
    assert not saved.exists()


def test_restoring_twice_is_harmless(device: _Recorder, tmp_path: Path) -> None:
    """The session's ``finally`` and the atexit hook both fire; the second finds nothing."""
    saved = tmp_path / consolefont.SAVED_NAME
    saved.write_bytes(b"PSF")
    consolefont.restore(saved)
    consolefont.restore(saved)
    consolefont.restore(None)
    assert device.calls == [["setfont", str(saved)]]


def test_a_setfont_that_fails_is_swallowed(
    device: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A font is cosmetic: a non-zero exit is a log line, never an exception."""
    monkeypatch.setattr(
        consolefont.subprocess,
        "run",
        _Recorder([subprocess.CompletedProcess(["setfont"], 1, "", "setfont: KDFONTOP: EINVAL")]),
    )
    assert consolefont.apply("6x8") is False


def test_a_setfont_that_cannot_be_run_is_swallowed(
    device: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same for a binary that vanishes between the which() and the exec, and for a timeout."""

    def _boom(argv: list[str], **kwargs: Any) -> Any:
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(consolefont.subprocess, "run", _boom)
    assert consolefont.apply("6x8") is False
    assert consolefont.remember() is None


def test_a_failed_remember_leaves_nothing_to_restore(
    device: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing saved means nothing to put back — the caller's ``finally`` gets a None."""
    monkeypatch.setattr(
        consolefont.subprocess,
        "run",
        _Recorder([subprocess.CompletedProcess(["setfont"], 1, "", "denied")]),
    )
    assert consolefont.remember() is None


# -- the preference --------------------------------------------------------------


def test_the_preference_is_offered_on_the_handheld_only() -> None:
    """A row about hardware the desktop does not have is a question it cannot answer."""
    spec = get_spec("console_font")
    assert spec.offered_on("picocalc-lyra") is True
    assert spec.offered_on("regular") is False
    # Everything else is offered everywhere; the gate is the exception, not the rule.
    gated = {s.key for _, specs in by_group() for s in specs if s.platforms is not None}
    assert gated == {"console_font"}


@pytest.mark.parametrize("platform", [REGULAR, PICOCALC_LYRA])
def test_the_preference_round_trips_on_every_platform(tmp_path: Path, platform: Any) -> None:
    """A file written on the handheld loads, keeps its value, and saves back on a desktop."""
    set_platform(platform)
    path = tmp_path / "preferences.toml"
    written = Preferences(path)
    written.set("console_font", "6x8")
    written.save()

    reread = Preferences.load(path)
    assert reread.get("console_font") == "6x8"
    # And saving again keeps it: the gate hides a row, it never drops a value.
    reread.save()
    assert "console_font" in path.read_text(encoding="utf-8")
    assert Preferences.load(path).get("console_font") == "6x8"


def test_the_page_draws_the_row_on_the_handheld_and_not_on_the_desktop() -> None:
    """The gate's one visible effect: a Display row that exists on one platform."""
    from meshterm.ui.preferences import _menu_items

    set_platform(PICOCALC_LYRA)
    _, items = _menu_items(Preferences(), {})
    assert "console_font" in [getattr(item, "value", None) for item in items]

    set_platform(REGULAR)
    _, items = _menu_items(Preferences(), {})
    assert "console_font" not in [getattr(item, "value", None) for item in items]


def test_the_printed_table_still_names_every_preference() -> None:
    """The CLI's listing is platform-blind on purpose: a hidden key is one nobody would set."""
    from meshterm.ui.preferences import preferences_table

    set_platform(REGULAR)
    console = Console(width=120, file=io.StringIO(), theme=active_theme(), legacy_windows=False)
    with console.capture() as capture:
        console.print(preferences_table(Preferences(), 120))
    assert "console_font" in capture.get()


# -- the wiring ------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path: Path) -> Any:
    """A mock-backed context whose preferences live in a throwaway file."""
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
    """`meshterm preferences set console_font 6x8` writes the file and touches nothing else.

    A scripted run was typed into a console MeshTerm was invited into and does not own —
    and half the time it is an ssh session onto the handheld from somewhere else entirely.
    """
    result = await PreferencesTool().run(ctx, {"ops": [("set", "console_font", "6x8")]})
    assert result.summary == {"changes": 1}
    assert ctx.preferences.get("console_font") == "6x8"
    assert device.calls == []


async def test_the_page_switches_the_font_as_it_saves(device: _Recorder, ctx: AppContext) -> None:
    """On the menu path the new font is loaded there and then, not next launch."""
    from meshterm.ui.surface import TuiUi

    ctx.ui = TuiUi(object())  # type: ignore[arg-type]
    await PreferencesTool().run(ctx, {"ops": [("set", "console_font", "6x8")]})
    assert device.calls == [["setfont", "/usr/share/consolefonts/meshterm8.psf.gz"]]


async def test_another_preference_leaves_the_font_alone(device: _Recorder, ctx: AppContext) -> None:
    """Only the one preference reaches outside the app; the rest just get written."""
    from meshterm.ui.surface import TuiUi

    ctx.ui = TuiUi(object())  # type: ignore[arg-type]
    await PreferencesTool().run(ctx, {"ops": [("set", "history_days", "30")]})
    assert device.calls == []
