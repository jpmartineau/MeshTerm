# SPDX-License-Identifier: Apache-2.0
"""The macOS Terminal profile: what it contains, and what it refuses to change.

The archive shapes in these tests are not invented. They were read from a profile that was
installed on a real Mac, and the profile was confirmed to render. Thus the tests fix the
encoder to bytes that are known to work. They do not fix it to an interpretation of the
format of Apple.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from meshterm.core import macterminal
from meshterm.core.relaunch import REOPENED_ENV
from meshterm.ui.theme import _VT_SLOTS


def test_color_archive_is_the_shape_appkit_writes() -> None:
    """The colour archive has the shape that AppKit writes.

    A colour decodes to a calibrated-RGB ``NSColor``, with six places and a NUL at the end.
    """
    decoded = plistlib.loads(macterminal.ns_color("#aa0000"))

    assert decoded["$archiver"] == "NSKeyedArchiver"
    assert decoded["$version"] == 100000
    colour = decoded["$objects"][1]
    assert colour["NSColorSpace"] == 1
    assert colour["NSRGB"] == b"0.666667 0.000000 0.000000\x00"
    assert decoded["$objects"][2]["$classname"] == "NSColor"


def test_font_archive_names_the_bundled_face() -> None:
    """The font archive names the bundled face.

    It has the PostScript name and not the family name.
    """
    decoded = plistlib.loads(macterminal.ns_font())

    assert macterminal.FONT_FACE in decoded["$objects"]
    assert decoded["$objects"][1]["NSSize"] == macterminal.FONT_SIZE
    assert decoded["$objects"][3]["$classname"] == "NSFont"


def test_every_palette_slot_comes_from_the_theme() -> None:
    """Each palette slot comes from the theme.

    The window cannot differ from the screen that it holds. The profile has sixteen keys,
    in the order that Terminal needs (dim, then bright). Each key has its own hex value in
    the theme. A change to a slot in :data:`~meshterm.ui.theme._VT_SLOTS` appears here at
    the next write. This is the reason that the code generates the profile, and does not
    ship it.
    """
    profile = macterminal.build_profile()

    assert len(macterminal._ANSI_KEYS) == len(_VT_SLOTS) == 16
    for key, (_slot, _label, rgb) in zip(macterminal._ANSI_KEYS, _VT_SLOTS, strict=True):
        assert profile[key] == macterminal.ns_color(rgb), key


def test_the_window_is_opaque_and_bold_is_not_brightness() -> None:
    """The window is opaque, and bold is not brightness.

    These are the two settings, with the font, that the profile exists to guarantee.
    """
    profile = macterminal.build_profile()

    assert profile["BackgroundColor"] == macterminal.ns_color("#000000")
    assert "BackgroundBlur" not in profile
    assert profile["UseBrightBold"] is False
    # The window closes after a clean exit. It stays after a crash, so that the user can
    # read the error.
    assert profile["shellExitAction"] == 1


def test_the_profile_is_the_same_bytes_whatever_the_command_line() -> None:
    """The profile is the same bytes for each command line.

    This test fixes the finding that shaped the module. Terminal names an imported
    settings set from the file. It uses the set again when the bytes are the same, and it
    adds "MeshTerm 1" when they are not. Thus the profile must not have anything that
    changes between starts. The launcher path is fixed, and the command that changes is in
    the file that the path points at.
    """
    import plistlib

    plain = plistlib.dumps(macterminal.build_profile("/Users/x/.meshterm/launch.sh"))
    again = plistlib.dumps(macterminal.build_profile("/Users/x/.meshterm/launch.sh"))

    assert plain == again
    # The value that goes in is a path. It is never an argv or an environment.
    profile = macterminal.build_profile("/Users/x/.meshterm/launch.sh")
    assert profile["CommandString"] == "/Users/x/.meshterm/launch.sh"
    assert "--mock" not in str(profile["CommandString"])
    assert "MESHTERM_" not in str(profile["CommandString"])


def test_the_launcher_carries_the_command_and_is_executable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher has the command and is executable.

    The part that changes is in the launcher instead, and the code generates it again at
    each start.
    """
    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "argv", ["meshterm", "--mock"])

    path = macterminal.write_launcher()
    body = path.read_text(encoding="utf-8")

    assert path.name == "launch.sh"
    assert body.startswith("#!/bin/sh")
    assert "exec /usr/bin/env " in body
    assert body.rstrip().endswith("--mock")
    if sys.platform != "win32":  # NTFS has no execute bit for chmod to set
        assert path.stat().st_mode & 0o111


def test_a_bare_profile_runs_nothing() -> None:
    """A profile with no command runs nothing.

    Without a command, the profile is an entry in the list and not a launcher.
    """
    profile = macterminal.build_profile()

    assert "CommandString" not in profile
    assert "RunCommandAsShell" not in profile


def test_the_profile_round_trips_as_a_terminal_file() -> None:
    """The profile makes a round trip as a Terminal file.

    What MeshTerm writes is what Terminal reads: a plist, with the nested archives
    unchanged.
    """
    written = plistlib.dumps(macterminal.build_profile("true"), fmt=plistlib.FMT_XML)
    reloaded = plistlib.loads(written)

    assert reloaded["name"] == macterminal.PROFILE_NAME
    assert reloaded["type"] == "Window Settings"
    assert reloaded["CommandString"] == "true"
    assert plistlib.loads(reloaded["ANSIRedColor"])["$objects"][1]["NSColorSpace"] == 1


def test_launch_command_carries_meshterm_variables_and_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch command has the MeshTerm variables and the marker.

    Terminal starts the command from a new shell, so what the command needs goes with it.
    """
    monkeypatch.setenv("MESHTERM_HOME", "/Users/someone/meshterm test")
    monkeypatch.delenv(REOPENED_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["meshterm", "--mock"])

    command = macterminal.launch_command()

    assert f"{REOPENED_ENV}=1" in command
    # The value is in quotes, because the path of the user can have a space, and the shell
    # would split it.
    assert "MESHTERM_HOME='/Users/someone/meshterm test'" in command
    assert command.endswith("--mock")
    # The command starts with a real executable and not a `VAR=value` shell prefix. A
    # measurement on macOS 26 showed this. Terminal reads the string as a bare program name
    # under one setting, and as a shell line under the other setting. A prefix puts
    # "Command not found: MESHTERM_REOPENED=1" on the screen.
    assert command.startswith("/usr/bin/env ")


def test_an_unrelated_variable_is_not_carried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launch command does not carry a variable that is not related to MeshTerm.

    The step to a new window must not copy the whole environment into a new shell.
    """
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")

    assert "AWS_SECRET_ACCESS_KEY" not in macterminal.launch_command()


def test_a_reopened_session_never_reopens_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session that was reopened never reopens again.

    This is the guard that stops a relaunch from chaining. The command of the profile sets
    the marker.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    monkeypatch.setenv(REOPENED_ENV, "1")

    assert macterminal.available() is False
    assert macterminal.reopen() is False


@pytest.mark.parametrize("program", ["iTerm.app", "ghostty", "vscode", "WezTerm"])
def test_another_terminal_is_left_alone(program: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The code leaves another terminal alone.

    MeshTerm makes no offer to a terminal that already has truecolor and a chosen font.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv(REOPENED_ENV, raising=False)
    monkeypatch.setenv("TERM_PROGRAM", program)

    assert macterminal.in_apple_terminal() is False
    assert macterminal.available() is False


def test_nothing_happens_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing happens off macOS.

    Each entry point does nothing on the platforms that this module is not for.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")

    assert macterminal.in_apple_terminal() is False
    assert macterminal.available() is False
    assert macterminal.reopen() is False
    assert macterminal.install_font() is False


def test_the_profile_is_written_under_meshterm_home(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The profile is in MeshTerm's own directory.

    It follows ``MESHTERM_HOME`` when that is set.
    """
    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path / "home"))

    path = macterminal.write_profile()

    assert path == tmp_path / "home" / "MeshTerm.terminal"
    assert plistlib.loads(path.read_bytes())["name"] == "MeshTerm"


def test_the_readers_preferences_are_never_named(tmp_path, monkeypatch) -> None:
    """The code never names the preferences of the user.

    Terminal discards external edits to its preferences when it quits, so no code here
    writes them. This test guards against an easy shortcut in the future. It does not guard
    against the code of today. The whole design depends on this: MeshTerm gives Terminal a
    file, and Terminal does the import.
    """
    source = (macterminal.__file__).replace(".pyc", ".py")
    with open(source, encoding="utf-8") as handle:
        body = handle.read().split('"""', 2)[2]

    assert "com.apple.Terminal.plist" not in body
    assert "Library/Preferences" not in body
    assert "defaults" not in body.replace("default_config_dir", "")


def test_the_font_goes_to_the_users_own_library() -> None:
    """The font goes to the user's own library.

    No administrator rights are necessary, and the user can undo it from Font Book.
    """
    assert macterminal.USER_FONT_DIR == Path.home() / "Library" / "Fonts"
