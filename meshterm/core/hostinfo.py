# SPDX-License-Identifier: Apache-2.0
"""The host that MeshTerm runs on, read one time, so that a bug report already has the answers.

Each value here answers a question that otherwise costs one more exchange with the person
who reported the problem: which build of MeshTerm, on which OS, in which terminal, how
wide, and with which colour depth. No value is about the mesh, and no value is private.
This module intentionally knows nothing about contacts, keys, positions, or messages.
Thus the user can paste the block that it fills in public, and nobody has to read the
block first.

Two probes are **ladders over environment variables**, not protocol queries. This is a
compromise, and we state it here: a terminal has no portable way to tell what it is. The
one sequence that comes close (``CSI > q``) needs a live tty, a reply that may never
arrive, and a timeout budget on a path that must never hang. Thus MeshTerm identifies
each terminal by the variable that the terminal sets about itself. This is the same
ladder that :func:`~meshterm.ui.termfont.detect_terminal_font` already uses for the font.
If MeshTerm does not recognize the terminal, it uses ``TERM``, not a guess.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: The first Windows 11 build. Windows 11 still reports its release as ``"10"`` through
#: each documented API. Thus the build number is the only thing that separates the two.
_WINDOWS_11_BUILD = 22000

#: The terminals that identify themselves, by the variable that each one sets. The order
#: is from the most specific to the least specific. An emulator inside another emulator
#: (the terminal of VS Code, a multiplexer) sets its own marker, and also inherits the
#: outer marker. Thus the inner identity must win.
#:
#: MeshTerm checks ``TERM_PROGRAM`` separately, between these variables and the ``TERM``
#: fallback, because the value of ``TERM_PROGRAM`` is the name. Thus it identifies
#: terminals that this table never has to list.
_TERMINAL_VARS: tuple[tuple[str, str], ...] = (
    ("WT_SESSION", "Windows Terminal"),
    ("ConEmuPID", "ConEmu"),
    ("TERMINAL_EMULATOR", ""),  # JetBrains IDEs put the name in the value
    ("KITTY_WINDOW_ID", "kitty"),
    ("ALACRITTY_WINDOW_ID", "Alacritty"),
    ("KONSOLE_VERSION", "Konsole"),
    ("GNOME_TERMINAL_SCREEN", "GNOME Terminal"),
    ("VTE_VERSION", "VTE-based"),
)

#: The names that ``TERM_PROGRAM`` gives to terminals whose own name is not the name that
#: a person uses. The table has only the names that are really different. Each other
#: value passes through as it is. Thus this table does not grow into a catalogue of all
#: the terminals that exist.
_TERM_PROGRAM_NAMES: dict[str, str] = {
    "Apple_Terminal": "Terminal.app",
    "vscode": "VS Code",
}


@dataclass(frozen=True, slots=True)
class Host:
    """The machine and the Python that run MeshTerm.

    Attributes:
        install: How this copy was installed. ``"frozen"``: a one-file build, which has
            its own bootloader, its own relaunch rules, and no source tree to read.
            ``"source"``: a checkout, so MeshTerm can ask for a commit. ``"package"``: a
            wheel from an index. The three fail in different ways, and the person who
            reports a problem usually does not know which one they have.
        os: The OS and its version, as a person writes it: ``Windows 11``,
            ``macOS 15.6``, ``Debian GNU/Linux 12 (bookworm)``.
        os_build: The exact build under that name: a Windows build number, or the kernel
            release on all other systems. This value separates two hosts that have the
            same name.
        arch: The processor architecture. It tells an arm64 build from an x86_64 build
            when a binary does not operate correctly.
        python: The interpreter version, in three parts.
    """

    install: str
    os: str
    os_build: str
    arch: str
    python: str


@dataclass(frozen=True, slots=True)
class Terminal:
    """The terminal in which MeshTerm draws, and what it can draw.

    Attributes:
        program: The name of the emulator. If nothing identified itself, the value of
            ``TERM``. ``None`` when ``TERM`` is not set either.
        term: The ``TERM`` value, exactly. It is kept next to ``program``, not merged
            into it, because a terminal that gives a false ``TERM`` is itself a frequent
            cause of a rendering report.
        colorterm: The ``COLORTERM`` value. A terminal uses it to claim truecolor. It is
            the first thing to check when a screen comes in the wrong colours.
        cols: The width of the terminal in cells.
        rows: The height of the terminal in cells.
        over_ssh: Whether this is an ssh session. It changes what MeshTerm can know (the
            font is on the remote client). It is also the reason that several other
            results here show ``unknown`` instead of a wrong value.
    """

    program: str | None
    term: str | None
    colorterm: str | None
    cols: int
    rows: int
    over_ssh: bool


def install_kind() -> str:
    """How this copy of MeshTerm was installed: ``frozen``, ``source``, or ``package``.

    A frozen build is clear, because the bootloader sets the flag. After that, the
    question is whether there is a repository above the package. MeshTerm can ask a
    checkout for its commit, and a checkout can have uncommitted work. A wheel can do
    neither.

    Returns:
        One of ``"frozen"``, ``"source"``, or ``"package"``.
    """
    if getattr(sys, "frozen", False):
        return "frozen"
    # meshterm/core/hostinfo.py -> meshterm/core -> meshterm -> the tree that holds it
    if (Path(__file__).resolve().parents[2] / ".git").exists():
        return "source"
    return "package"


def host() -> Host:
    """Read the facts of the machine.

    Returns:
        The :class:`Host` block.
    """
    name, build = _os_name_and_build()
    return Host(
        install=install_kind(),
        os=name,
        os_build=build,
        arch=platform.machine() or "unknown",
        python=platform.python_version(),
    )


def terminal(environ: Mapping[str, str] | None = None) -> Terminal:
    """Read the facts of the terminal.

    Args:
        environ: The environment to examine (the default is ``os.environ``). A test can
            inject it, so that the ladder can be tested without a terminal of the correct
            type.

    Returns:
        The :class:`Terminal` block.
    """
    env = os.environ if environ is None else environ
    size = shutil.get_terminal_size()
    return Terminal(
        program=terminal_program(env),
        term=env.get("TERM") or None,
        colorterm=env.get("COLORTERM") or None,
        cols=size.columns,
        rows=size.lines,
        over_ssh=bool(env.get("SSH_TTY") or env.get("SSH_CONNECTION")),
    )


def terminal_program(environ: Mapping[str, str] | None = None) -> str | None:
    """Name the terminal emulator from what it tells about itself.

    The ladder: first, the terminals in :data:`_TERMINAL_VARS` that set a marker
    variable. Then ``TERM_PROGRAM``, whose value is the name, so that it covers terminals
    that this module does not know. Last, ``TERM``, which is the last honest answer that
    there is.

    Args:
        environ: The environment to examine (the default is ``os.environ``).

    Returns:
        The name of the terminal, or ``None`` when nothing in the environment names one.
    """
    env = os.environ if environ is None else environ
    for var, name in _TERMINAL_VARS:
        value = env.get(var)
        if value:
            return name or value
    program = env.get("TERM_PROGRAM")
    if program:
        return _TERM_PROGRAM_NAMES.get(program, program)
    return env.get("TERM") or None


def _os_name_and_build() -> tuple[str, str]:
    """The OS as a person writes it, and the exact build under that name."""
    system = platform.system()
    if system == "Windows":
        return _windows()
    if system == "Darwin":
        version = platform.mac_ver()[0]
        return (f"macOS {version}" if version else "macOS"), platform.release()
    if system == "Linux":
        return (_pretty_name() or "Linux"), platform.release()
    return (system or "unknown"), platform.release()


def _windows() -> tuple[str, str]:
    """Windows by the name under which it is sold, and its build number.

    ``platform.win32_ver`` reports release ``"10"`` also on Windows 11, because the new
    name never got into the version APIs. Thus the build number decides which Windows
    this is. The build is also the useful half of the pair, because a fix is shipped for
    a build.
    """
    release, version, _service_pack, _kind = platform.win32_ver()
    try:
        build = int(version.rsplit(".", 1)[-1])
    except ValueError:
        build = 0
    name = "Windows 11" if build >= _WINDOWS_11_BUILD else f"Windows {release or '?'}"
    return name, version or platform.version()


def _pretty_name() -> str | None:
    """The name that the distribution gives itself, from ``/etc/os-release``.

    This is the only place where the system itself states its Linux variant, so that
    MeshTerm does not have to guess. It is the reason that a report can show "Calculinux"
    or "Raspberry Pi OS" instead of ``Linux``. A host without the file (or without
    permission to read it) has no answer.
    """
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key == "PRETTY_NAME":
            return value.strip().strip('"') or None
    return None
