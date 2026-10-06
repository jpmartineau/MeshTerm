# SPDX-License-Identifier: Apache-2.0
"""Open the session again in a Terminal window that looks like MeshTerm.

The problem with macOS Terminal is not the same as the problem with the classic Windows
console. Terminal uses fallback fonts freely: when its font does not have a glyph, it finds
the glyph in a different font. Thus no glyph is ever drawn as a box. The problem is the
font that Terminal finds. No Mac monospace font has the Braille Patterns block at all (SF
Mono, Menlo, Monaco, and Courier all measure zero). The charts and the terrain of the map
are drawn from that block. Thus Terminal gets the block from a **proportional** font, and
the glyphs have the wrong width.

The screen is not empty, but it is crooked, and this is worse. Nothing shows that it is
broken, and the user has nothing to compare it with.

The solution is a font, and a font must have a Terminal profile that names it. This causes
the question that this module exists to answer carefully: **whose terminal is it?**

The palette of MeshTerm is the 16-colour VT set (:data:`~meshterm.ui.theme._VT_SLOTS`). It
is a crude set of colours, like CGA, and the app uses it on purpose. But a user must not
have to use these colours to read their mail. Thus this module never changes the default
profile of the user, and never edits their Terminal preferences. It only adds a profile with
the name MeshTerm, and opens the window of MeshTerm in that profile. The Terminal of the
user keeps all their choices, also its translucency. Our window is opaque and crude, and it
is a separate window. When the user quits MeshTerm, nothing stays.

This design also avoids a trap. We write the trap down, because it takes an evening to
find. When Terminal quits, it writes ``~/Library/Preferences/com.apple.Terminal.plist``
again **from its memory**. Thus, if a program writes into that file while Terminal runs,
Terminal discards that change later, with no message. And MeshTerm always runs in
Terminal. No code in this module writes that file. MeshTerm gives a ``.terminal`` file to
Terminal, and Terminal itself does the import. This is the only path that works from a
live session.

MeshTerm generates the profile, instead of a file in the package. The profile comes from
the same theme constant that the app uses to draw itself. Thus the window can never
disagree with the screen in it.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .relaunch import REOPENED_ENV, own_command, wanted_size

#: The name under which the profile is installed, and the name that the user sees in the
#: profile list of Terminal. It is stable: if we rename it, the copy that is already on the
#: machine of the user stays there with no use.
PROFILE_NAME = "MeshTerm"

#: The bundled font, by its PostScript name. An ``NSFont`` archive holds this name, not the
#: family name that Font Book shows. :func:`install_font` installs the font from the copy
#: in the package. Refer to :mod:`meshterm.core.consolefont` for the Windows part of the
#: same idea.
FONT_FACE = "CascadiaMonoPL-Regular"

#: The size in points. The default of Terminal is 11. At 13, the braille charts are clear
#: on a Retina display, and the map does not lose rows.
FONT_SIZE = 13.0

#: The directory for a font that the user installs on macOS. No administrator rights and
#: no installer are necessary. The user can undo the install: drag the file out of Font
#: Book.
USER_FONT_DIR = Path("~/Library/Fonts").expanduser()

#: The Terminal plist key for each palette slot, in the order of
#: :data:`~meshterm.ui.theme._VT_SLOTS`: the 16 standard ANSI colours, first the dim bank,
#: then the bright bank.
_ANSI_KEYS: tuple[str, ...] = (
    "ANSIBlackColor",
    "ANSIRedColor",
    "ANSIGreenColor",
    "ANSIYellowColor",
    "ANSIBlueColor",
    "ANSIMagentaColor",
    "ANSICyanColor",
    "ANSIWhiteColor",
    "ANSIBrightBlackColor",
    "ANSIBrightRedColor",
    "ANSIBrightGreenColor",
    "ANSIBrightYellowColor",
    "ANSIBrightBlueColor",
    "ANSIBrightMagentaColor",
    "ANSIBrightCyanColor",
    "ANSIBrightWhiteColor",
)


def _archive(objects: list[object]) -> bytes:
    """One ``NSKeyedArchiver`` document: the form of each colour and font in Terminal.

    Terminal keeps these as nested binary plists in the profile: an archived ``NSColor``
    or ``NSFont``, not a number and a string. This is because AppKit writes the profile,
    and AppKit did not make this format for other programs. The structure is small and
    fixed, so this module builds it, and does not get it from a dependency.

    Args:
        objects: The ``$objects`` array, with ``$null`` already at index 0.

    Returns:
        The encoded archive. It is binary, because :class:`plistlib.UID` has no XML form.
    """
    return plistlib.dumps(
        {
            "$archiver": "NSKeyedArchiver",
            "$objects": objects,
            "$top": {"root": plistlib.UID(1)},
            "$version": 100000,
        },
        fmt=plistlib.FMT_BINARY,
    )


def ns_color(rgb: str) -> bytes:
    """An archived ``NSColor`` for a ``#rrggbb`` string.

    Args:
        rgb: The colour, in the form that the theme uses.

    Returns:
        The archive that Terminal expects in a colour key.
    """
    channels = (int(rgb[index : index + 2], 16) / 255 for index in (1, 3, 5))
    # Calibrated RGB, with spaces between the values, six decimal places, and a NUL at the
    # end. This is the form that AppKit itself writes. We match it exactly, because
    # Terminal parses it strictly and does not accept a different form.
    packed = " ".join(f"{value:.6f}" for value in channels).encode("ascii") + b"\x00"
    return _archive(
        [
            "$null",
            {"$class": plistlib.UID(2), "NSColorSpace": 1, "NSRGB": packed},
            {"$classes": ["NSColor", "NSObject"], "$classname": "NSColor"},
        ]
    )


def ns_font(face: str = FONT_FACE, size: float = FONT_SIZE) -> bytes:
    """An archived ``NSFont``.

    Args:
        face: The PostScript name of the font.
        size: The size in points.

    Returns:
        The archive that Terminal expects in the ``Font`` key.
    """
    return _archive(
        [
            "$null",
            {
                "$class": plistlib.UID(3),
                "NSName": plistlib.UID(2),
                "NSSize": float(size),
                "NSfFlags": 16,
            },
            face,
            {"$classes": ["NSFont", "NSObject"], "$classname": "NSFont"},
        ]
    )


def launch_command() -> str:
    """The shell line that starts this session again, for the profile to run.

    Terminal starts the command from a **new login shell**. This is why the command is a
    string and not an argv, and why it carries explicitly all that it needs. No part of the
    environment of the current process goes through to the new shell. This is good for the
    ``_PYI*`` variables, which a frozen build must not give to its child (refer to
    :func:`~meshterm.core.relaunch.child_environment`, which exists for Windows, where these
    variables do go through). But it is bad for a ``MESHTERM_HOME`` that the user set for
    only this run, which the README itself tells the user to do. Thus the command carries
    each ``MESHTERM_*`` variable that is set now, and also the marker of a reopened session.

    The variables go on ``/usr/bin/env``, not on a ``VAR=value`` prefix, and this choice is
    not a matter of style. We cannot trust how Terminal reads the string. A measurement on
    macOS 26 showed that the same command line runs when ``RunCommandAsShell`` is false,
    and fails when it is true. When it is true, Terminal uses the full string as the name
    of a program, and shows this on the screen::

        Command not found: MESHTERM_REOPENED=1
        Could not create a new process and open a pseudo-tty

    ``env`` is a real executable, so the line is a valid argv and also a valid shell line.
    Thus the window cannot show that message, however Terminal reads the line. ``env``
    execs the program in its own place, so nothing stays behind it. The window holds
    MeshTerm and nothing else, and when MeshTerm exits, the window does not go to a shell
    prompt.

    Returns:
        A command line that Terminal can run with or without a shell.
    """
    carried = {name: value for name, value in os.environ.items() if name.startswith("MESHTERM_")}
    carried[REOPENED_ENV] = "1"
    assignments = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in sorted(carried.items())
    )
    program = " ".join(shlex.quote(part) for part in own_command())
    return f"/usr/bin/env {assignments} {program}"


def build_profile(command: str | None = None) -> dict[str, object]:
    """The MeshTerm profile, in the form that the Terminal preferences hold it.

    Each colour comes from :data:`~meshterm.ui.theme._VT_SLOTS`. Thus the window and the
    app in it cannot disagree: if you change the theme, the profile follows at the next
    write. The window size comes from :func:`~meshterm.core.relaunch.wanted_size`, the same
    size that MeshTerm asks Windows Terminal for.

    Args:
        command: A shell line for the window to run when it opens. ``None`` gives a
            profile that only stays in the list until somebody selects it.

    Returns:
        The profile dictionary, ready to write as a ``.terminal`` file.
    """
    from ..ui.theme import _VT_SLOTS

    cols, rows = wanted_size()
    profile: dict[str, object] = {
        "name": PROFILE_NAME,
        "type": "Window Settings",
        "ProfileCurrentVersion": 2.07,
        "Font": ns_font(),
        "FontAntialias": True,
        "FontWidthSpacing": 1.0,
        # Opaque on purpose. A braille raster on a translucent background cannot be read.
        # But a translucent profile is a reasonable choice for the user to have. This is
        # the full reason why MeshTerm has a window of its own.
        "BackgroundColor": ns_color("#000000"),
        "TextColor": ns_color("#aaaaaa"),
        "TextBoldColor": ns_color("#ffffff"),
        "SelectionColor": ns_color("#555555"),
        "CursorColor": ns_color("#55ffff"),
        "CursorType": 0,
        "BlinkText": False,
        # Bold is bold, not a move to the bright bank. The theme gives brightness as a
        # colour (refer to the note on the wordmark). If Terminal also promotes bold text,
        # the colour goes one bank too high.
        "UseBrightBold": False,
        "columnCount": cols,
        "rowCount": rows,
        "ScrollbackLines": 10000,
        "ShouldLimitScrollback": 0,
        "ShowActiveProcessInTitle": True,
        "ShowDimensionsInTitle": False,
        "ShowWindowSettingsNameInTitle": False,
        # Close the window when MeshTerm exits cleanly, and keep it open when it does not.
        # If the user never sees a crash, nobody can write a bug report about it.
        "shellExitAction": 1,
    }
    for key, (_slot, _label, rgb) in zip(_ANSI_KEYS, _VT_SLOTS, strict=True):
        profile[key] = ns_color(rgb)
    if command is not None:
        profile["CommandString"] = command
        # False, measured. When it is true, Terminal uses the full string as a program
        # name, and the window shows "Command not found: ...". When it is false, the
        # command runs. This is the opposite of what the name of the key suggests, so we
        # write it down.
        profile["RunCommandAsShell"] = False
    return profile


def launcher_path() -> Path:
    """The small script that the profile runs, next to the profile itself."""
    from .config import default_config_dir

    return default_config_dir() / "launch.sh"


def write_launcher() -> Path:
    """Write the launcher, and return its path.

    This function exists for one reason, and this reason is the finding that gave this
    module its design. Terminal names an imported profile **after the file**:
    ``MeshTerm.terminal`` becomes the profile "MeshTerm". If you import the same bytes
    again, Terminal uses that profile again. But if you import different bytes with the
    same file name, Terminal makes "MeshTerm 1", then "MeshTerm 2". We measured it: we
    opened one file four times. Three identical imports left one profile. The fourth, with
    one word changed, left two.

    Thus the command cannot be in the profile. The command carries ``sys.argv`` and the
    ``MESHTERM_*`` variables of the user, which are different for ``meshterm`` and
    ``meshterm --mock``. A profile for each command line fills the profile list of the user
    with junk that only the user can remove.

    A fixed path in front of the part that changes solves this problem exactly. The profile
    names ``~/.meshterm/launch.sh`` and never changes. This file behind that path holds the
    command that this run needs.

    Returns:
        The path that was written. The file is executable.
    """
    path = launcher_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "# Written by MeshTerm each time it reopens itself. Safe to delete.\n"
        f"exec {launch_command()}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def profile_path() -> Path:
    """The path where MeshTerm keeps the generated ``.terminal`` file.

    The file is in the directory of MeshTerm, not on the Desktop. The file is ours. This
    directory is the one place that already stays after an upgrade, and it moves with
    ``MESHTERM_HOME``.
    """
    from .config import default_config_dir

    return default_config_dir() / f"{PROFILE_NAME}.terminal"


def write_profile(*, command: str | None = None) -> Path:
    """Write the profile, and return its path.

    Args:
        command: A shell line for the window to run, or ``None`` for a profile with no
            command.

    Returns:
        The path that was written.
    """
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build_profile(command), fmt=plistlib.FMT_XML))
    return path


def font_installed() -> bool:
    """Whether the bundled font is already in the font directory of the user."""
    from .consolefont import BUNDLED_FONT

    return (USER_FONT_DIR / BUNDLED_FONT.name).is_file()


def install_font() -> bool:
    """Copy the bundled font into the font directory of the user.

    This is the only part of this module that writes outside the folder of MeshTerm. This
    is why it is the only part that is worth a question to the user. It is one file, in
    the Library of the user. No administrator rights and no installer are necessary, and
    the user can remove the file: drag it out of Font Book. It does not change how the
    Terminal of the user looks. It only makes the font available, so that a profile can
    name it.

    It is safe to do again: if a copy exists, the function does not change it.

    Returns:
        Whether the font is installed and usable. This includes the case where it was
        already installed.
    """
    from .consolefont import BUNDLED_FONT

    if sys.platform != "darwin" or not BUNDLED_FONT.is_file():
        return False
    target = USER_FONT_DIR / BUNDLED_FONT.name
    if target.is_file():
        return True
    try:
        USER_FONT_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BUNDLED_FONT, target)
    except OSError:
        return False
    return True


def in_apple_terminal() -> bool:
    """Whether this session draws into macOS Terminal.

    iTerm2, Ghostty, kitty, WezTerm, and VS Code all set ``TERM_PROGRAM`` to their own
    name. Several of them have truecolor and also a font that the user chose on purpose.
    None of them needs anything that this module offers.
    """
    return sys.platform == "darwin" and os.environ.get("TERM_PROGRAM") == "Apple_Terminal"


def available() -> bool:
    """Whether it is useful here to open MeshTerm again in its own window.

    ``False`` in a session that is itself a reopened session. This stops a reopened
    session from opening one more session, in a chain. The command of the profile carries
    the marker, because Terminal starts the command from a new shell that gets nothing
    from us.
    """
    return in_apple_terminal() and not os.environ.get(REOPENED_ENV)


def reopen() -> bool:
    """Open MeshTerm in a window with its own profile, and report whether it started.

    ``open`` gives the file to Terminal, and **Terminal** imports the profile and opens
    the window. This is the only path that works from a live session, because Terminal
    discards the edits that other programs make to its preferences when it quits.

    In practice, the new window is not a child: the caller exits immediately after, and
    the two never communicate.

    There are two files, not one. The launcher carries the parts that change, and the
    profile points to the launcher. Thus the bytes of the profile are the same at each
    start, and Terminal uses the same profile again instead of a numbered copy.
    :func:`write_launcher` gives the measurement.

    Returns:
        Whether MeshTerm started ``open``. ``False`` leaves the caller exactly where it
        was. Thus a failure here only causes a different path, and never a dead end.
    """
    if not available():
        return False
    try:
        launcher = write_launcher()
        path = write_profile(command=str(launcher))
    except OSError:
        return False
    try:
        subprocess.Popen(  # noqa: S603 - the argv is ours, not the user's
            ["/usr/bin/open", str(path)],
            close_fds=True,
        )
    except OSError:
        return False
    return True
