# SPDX-License-Identifier: Apache-2.0
"""Open the session again in Windows Terminal, where MeshTerm looks as it was designed.

MeshTerm draws emoji icons, braille charts, and powerline path chips. The terminal decides
if these glyphs get to the screen. MeshTerm does not decide it, and the font does not
really decide it either. When a modern terminal must draw a character that its font does
not have, it gets the glyph from another font on the machine, and it does not tell the
user. This behaviour is the reason that the app looks correct in Windows Terminal, in the
terminal of VS Code, and on macOS and Linux. It also looks correct there when the font
that the user chose has almost none of these glyphs, which is usually the case. We
measured the frequent choices on a development machine (Hack Nerd Font, JetBrains Mono,
Fira Code, Source Code Pro): they have no braille at all. No monospace font anywhere has
emoji.

The classic Windows console does not do that. It draws the glyphs that its one font has,
and it draws boxes for all the other characters. Thus on that host, the emoji cannot show
at all. The only two fonts on a Windows machine with emoji glyphs (Segoe UI Emoji and
Segoe UI Symbol) are proportional, and a console does not accept a proportional font. No
font choice corrects this problem. Only one thing corrects it: a different terminal.

Thus, when MeshTerm finds that it runs in that console and Windows Terminal is installed,
it offers to open itself again in Windows Terminal. Windows Terminal is on each Windows 11
machine, and it is a free install on Windows 10. After one key press, the app looks as it
was drawn, not as conhost can show it. If the user declines, MeshTerm uses
:mod:`meshterm.core.consolefont`, which gets the best result from the console that the
user chose to stay in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .relaunch import REOPENED_ENV, child_environment, own_command, wanted_size

__all__ = [
    "REOPENED_ENV",
    "available",
    "child_environment",
    "own_command",
    "reopen",
]


def available() -> str | None:
    """The path to ``wt.exe``, or ``None`` when Windows Terminal is not installed.

    The function looks for it on ``PATH``. Windows puts an execution alias for Windows
    Terminal there, in ``%LOCALAPPDATA%/Microsoft/WindowsApps``.
    """
    if sys.platform != "win32" or os.environ.get(REOPENED_ENV):
        return None
    return shutil.which("wt.exe")


def reopen() -> bool:
    """Start this session again in Windows Terminal, in the same directory.

    The new window is not a child process in a useful sense. The caller exits immediately
    after this call, and the two processes never communicate. Thus the function waits for
    nothing and keeps no pipes.

    Returns:
        Whether Windows Terminal started. ``False`` leaves the caller exactly where it was.
        Thus the offer cannot leave the user without a session. If it fails, the session
        continues in this terminal.
    """
    terminal = available()
    if terminal is None:
        return False
    try:
        # `--` stops the option parsing of wt. Thus the flags of MeshTerm go to MeshTerm,
        # and Windows Terminal does not read them as its own flags (checked: `--mock` and
        # `--profile x` arrive unchanged). `-d` keeps the working directory, because the
        # relative paths on the command line depend on it.
        subprocess.Popen(  # noqa: S603 - the argv is ours, not the user's
            [
                terminal,
                "-w",
                "new",
                "--size",
                _wanted_size(),
                # Without this title, the title of the tab is the full path of the
                # executable. On a downloaded build, that path fills a line with the
                # Downloads folder. MeshTerm cannot set the icon: Windows Terminal gets the
                # icon from a profile, and a command line alone is not a profile. Thus the
                # tab gets the generic console glyph.
                "--title",
                "MeshTerm",
                "-d",
                os.getcwd(),
                "--",
                *own_command(),
            ],
            env=child_environment(),
            close_fds=True,
        )
        return True
    except OSError:
        return False


def _wanted_size() -> str:
    """The window size to ask Windows Terminal for, as ``cols,rows``.

    MeshTerm asks for a new window of a known size. Without ``-w new``, the session opens
    as a tab in the window that is open at that time, and it gets the size of that window.
    Without ``--size``, a new window gets the global launch size of the user. That size is
    for the shell of the user, not for this app. The dimensions come from
    :func:`~meshterm.core.relaunch.wanted_size`, which all the other terminals that
    MeshTerm opens itself again in also use. Only the format is specific to Windows
    Terminal.
    """
    cols, rows = wanted_size()
    return f"{cols},{rows}"
