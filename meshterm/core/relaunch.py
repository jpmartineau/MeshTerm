# SPDX-License-Identifier: Apache-2.0
"""What each relaunch of MeshTerm needs, for each terminal into which it relaunches.

MeshTerm opens itself again when the terminal from which it started cannot draw it. There
are two such terminals, for two unrelated reasons. The classic Windows console has no font
fallback, so an icon that its font does not have is a box, and no font choice corrects it
(:mod:`meshterm.core.winterminal`). macOS Terminal has the opposite problem: its fallback
is too generous. No Mac monospace face has the Braille Patterns block. Thus the charts come
from a proportional face, and their width is wrong (:mod:`meshterm.core.macterminal`). One
host shows nothing, and the other shows something crooked.

The destinations are very different, but the action is the same. Find the command that
starts this session again, give it an environment in which a new start can survive, and
let the caller exit. Thus this code is here, one time, and each platform module supplies
only the part that is really its own: which terminal, and how to ask it.
"""

from __future__ import annotations

import os
import sys

#: Set in the reopened session, so that a relaunch can never start another relaunch. The
#: gate of each platform usually refuses a second pass anyway. (Windows Terminal is not a
#: classic console. A window that opened from the profile of MeshTerm is already in that
#: profile.) But this variable is a second safeguard for those gates, and it costs one
#: environment variable.
REOPENED_ENV = "MESHTERM_REOPENED"


def own_command() -> list[str]:
    """The command that starts this same session again.

    A frozen build is its own executable, and takes the arguments as they were given. A
    development install or a ``pip`` install starts through its interpreter, because
    MeshTerm cannot be sure that the receiving terminal resolves the path of the console
    script.

    Returns:
        The program and its arguments, ready to give to a terminal.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *sys.argv[1:]]
    return [sys.executable, "-m", "meshterm", *sys.argv[1:]]


def child_environment() -> dict[str, str]:
    """The environment for a new start of MeshTerm, without the bookkeeping of this bundle.

    A PyInstaller one-file build runs in two stages. The executable unpacks itself into a
    temporary directory, and then runs itself again as a child. The two halves coordinate
    through ``_PYI*`` environment variables. These variables must not get to a new start of
    the executable. Else the bootloader finds them, and concludes that it is the second
    stage of a start that it never made. Then it checks that its parent is the same
    program, finds the terminal instead, and aborts::

        [PYI-33368:ERROR] Security validation failure: parent process has different
        executable!

    Then the full session is lost. A new window shows a message that nobody can act on,
    and the window that offered the move is already closed. When the function removes these
    variables, the new process is a first stage, which is its correct stage.

    Returns:
        The environment to start the new process with.
    """
    environment = dict(os.environ)
    environment[REOPENED_ENV] = "1"
    if not getattr(sys, "frozen", False):
        return environment

    for key in [key for key in environment if key.startswith("_PYI")]:
        del environment[key]
    environment.pop("_MEIPASS2", None)  # the name that older bootloaders used
    # The bootloader sets the loader path to the unpacked bundle, and keeps the original
    # value of the caller next to it. The new process unpacks its own copy, so it must
    # have the original value.
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        original = environment.pop(f"{name}_ORIG", None)
        if original is not None:
            environment[name] = original
        else:
            environment.pop(name, None)
    return environment


def wanted_size() -> tuple[int, int]:
    """The window size to ask for, as ``(cols, rows)``.

    MeshTerm asks for a size, because a window that MeshTerm opens must be a window in
    which MeshTerm fits. A session can open at the size with which the shell of the user
    starts, and that size can be too small for the app. The failure is quiet and cosmetic.
    The startup wordmark is 71 cells wide, so at 70 cells MeshTerm silently uses the narrow
    wordmark that was drawn for the PicoCalc. Also, a short window truncates each
    description onto the pager.

    The numbers are the minimum that the platform itself states, plus a margin. Thus they
    follow that minimum, and do not repeat it. A terminal can limit the size to what it can
    show, so this is a request and not a demand. On a small screen, the app degrades
    exactly as it does without the request.

    Returns:
        The width in cells and the height in rows to ask the terminal for.
    """
    from ..platforms import get_platform

    platform = get_platform()
    return platform.readable_cols + 8, platform.readable_rows + 6
