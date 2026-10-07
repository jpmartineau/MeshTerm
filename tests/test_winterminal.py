# SPDX-License-Identifier: Apache-2.0
"""Tests for the reopen of the session in Windows Terminal.

No test here starts a terminal. The tests check the command that the code would run. This
is the part that can be wrong with no message. A mistake in it makes a window that opens,
fails, and closes faster than anyone can read it.
"""

from __future__ import annotations

import sys

import pytest

from meshterm.core import winterminal


def test_it_is_only_ever_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reopen is only for Windows.

    The terminal of each other platform already draws the whole app. There is nothing to
    correct.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    assert winterminal.available() is None
    assert winterminal.reopen() is False


def test_a_reopened_session_never_offers_to_reopen_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session that was reopened never offers to reopen again.

    This is the loop guard. Windows Terminal does not qualify in any case, and the guard is
    the backup. A relaunch that can chain is not only a cosmetic bug. Each relaunch starts
    a window and exits, so the user would see windows open without end.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv(winterminal.REOPENED_ENV, "1")
    assert winterminal.available() is None
    assert winterminal.reopen() is False


def test_a_frozen_build_relaunches_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frozen build relaunches itself.

    The executable of the installer gets the arguments exactly as the user gave them.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Downloads\meshterm.exe")
    monkeypatch.setattr(sys, "argv", ["meshterm", "--mock", "--profile", "home"])
    assert winterminal.own_command() == [
        r"C:\Downloads\meshterm.exe",
        "--mock",
        "--profile",
        "home",
    ]


def test_a_source_install_relaunches_through_its_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source install relaunches through its interpreter, with ``-m meshterm``.

    The code does not use the console script. The path of the script is a wrapper, and its
    location depends on the install method (venv, pipx, user site). The code already knows
    the interpreter that runs it. The use of the interpreter also makes sure that the new
    window gets the same environment as this window.
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\venv\Scripts\python.exe")
    monkeypatch.setattr(sys, "argv", ["meshterm", "--mock"])
    assert winterminal.own_command() == [r"C:\venv\Scripts\python.exe", "-m", "meshterm", "--mock"]


def test_the_program_name_is_never_passed_on_as_an_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The code never passes the program name on as an argument.

    ``argv[0]`` is the name of this process. It is not for the next process. If the code
    passed it on, it would reach MeshTerm as an extra positional argument, and Typer would
    reject it. This would happen in a new window that then closes.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\meshterm.exe")
    monkeypatch.setattr(sys, "argv", [r"C:\some\other\name.exe"])
    assert winterminal.own_command() == [r"C:\meshterm.exe"]


def test_the_bundles_own_bookkeeping_never_reaches_the_new_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The own bookkeeping of the bundle never reaches the new process.

    The frozen build found this bug, and the source build could not find it. A PyInstaller
    executable of one file unpacks itself and runs itself again. The two halves talk
    through ``_PYI*`` variables. When the code passed these variables on, the reopened
    MeshTerm thought that it was the second half of a start that it never made. It checked
    its parent and found Windows Terminal. Then it stopped itself with a bootloader error,
    in a window that had just opened.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", r"C:\Downloads\meshterm.exe")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "0")
    monkeypatch.setenv("_MEIPASS2", r"C:\Temp\_MEI123")
    monkeypatch.setenv("MESHTERM_HOME", r"C:\mine")

    environment = winterminal.child_environment()

    assert not [key for key in environment if key.startswith("_PYI")]
    assert "_MEIPASS2" not in environment
    assert environment["MESHTERM_HOME"] == r"C:\mine"  # the own settings of the user stay
    assert environment[winterminal.REOPENED_ENV] == "1"


def test_a_source_run_keeps_the_environment_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source run keeps the environment that it got.

    The code removes nothing when there is no bundle, so a development run passes the whole
    environment on. ``LD_LIBRARY_PATH`` is the variable to watch. When the build is not
    frozen, the value belongs to the user. If the code removed it, the new process would
    load its libraries in a different way, and there would be no reason for this.
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/mine/lib")
    assert winterminal.child_environment()["LD_LIBRARY_PATH"] == "/opt/mine/lib"


def test_the_loader_path_the_bootloader_replaced_is_put_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The code puts back the loader path that the bootloader replaced.

    The bootloader points the loader at its own unpacked copy, and it keeps the original
    value in another variable. The new process unpacks a bundle of its own. Thus it needs
    the value that the user started with. If there was no value, it gets no value.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI999")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/mine/lib")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/tmp/_MEI999")
    monkeypatch.delenv("DYLD_LIBRARY_PATH_ORIG", raising=False)

    environment = winterminal.child_environment()

    assert environment["LD_LIBRARY_PATH"] == "/opt/mine/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in environment
    assert "DYLD_LIBRARY_PATH" not in environment


def test_the_window_asked_for_fits_the_startup_wordmark() -> None:
    """The window that the code asks for fits the startup wordmark.

    The size request exists because a window that is too small fails with no message. The
    startup wordmark is 71 columns wide. In a narrower window, the splash changes with no
    message to the narrow wordmark that is drawn for the PicoCalc. This is how the problem
    was found: a desktop session showed the mark of the handheld. The request must be wider
    than 71 columns, and not only wider than the minimum of the platform.
    """
    from meshterm.platforms import REGULAR, set_platform
    from meshterm.ui.logo import load_logo, logo_width

    set_platform(REGULAR)
    cols, rows = (int(part) for part in winterminal._wanted_size().split(","))

    assert logo_width(load_logo(cols)) == 71, "the window asked for would get the narrow mark"
    assert cols >= REGULAR.readable_cols
    assert rows >= REGULAR.readable_rows


def test_the_session_gets_its_own_window_at_that_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """The session gets its own window at that size.

    Both parts are necessary, and each was a separate way to get a window that is too
    small. Without ``-w new``, the session becomes a tab in a window that is already open,
    and it has the size of that window. A measurement on a real machine gave 64 columns.
    This is the reason that the wrong mark appeared. Without ``--size``, a new window gets
    the global start size of the user. That is a setting for the shell of the user, and not
    for this app.
    """
    started: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv(winterminal.REOPENED_ENV, raising=False)
    monkeypatch.setattr(winterminal.shutil, "which", lambda _: r"C:\wt.exe")
    monkeypatch.setattr(
        winterminal.subprocess, "Popen", lambda argv, **_: started.append(argv) or None
    )

    assert winterminal.reopen() is True
    argv = started[0]
    assert argv[:3] == [r"C:\wt.exe", "-w", "new"]
    assert "--size" in argv
    assert argv[argv.index("--size") + 1] == winterminal._wanted_size()
    # `--` must still separate the options of wt from the options of MeshTerm. If it does
    # not, `--mock` becomes an option of wt.
    assert "--" in argv and argv.index("--") > argv.index("--size")
