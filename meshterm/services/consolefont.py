# SPDX-License-Identifier: Apache-2.0
"""The console font of the PicoCalc: remember the old font, change it, and put it back.

The handheld draws to a Linux virtual terminal (VT), and a VT loads exactly one font for
the full console. There is no font for each application, as a desktop terminal emulator
has. Thus, when MeshTerm asks for its 6x8 build (53x40, fourteen more rows), it changes
the font that the shell uses. If MeshTerm leaves the font changed, the console of the user
stays changed after MeshTerm quits. This is the reason for the shape of this module:
**remember** the font that the shell had, **apply** the font that the preference names,
and **restore** the old font at each exit from the app.

``setfont -O`` writes the current font to a file, with its unicode table. When
``setfont`` reads that file again, it makes exactly the same font (checked on the
handheld). No ``sudo`` is necessary: the VT is the controlling terminal of the process,
and the user who is logged in owns it. That is the only reason that this font can be a
preference and not a setup step. MeshTerm never gives ``-C``, on purpose. Thus each call
goes to that controlling VT, and not to the tty number that is first.

All of this module is cosmetic, and all of it fails without a message to the user. A
missing ``setfont``, a read-only state directory, an ssh session that is not a VT, or a
non-zero exit: each one gives one debug line and a return that does nothing. A font is
never a reason for the app not to start. :func:`applies` is the single gate that all
three verbs ask first. Thus only one place answers "this is not that machine".

This module is different from :mod:`meshterm.core.consolefont`, which installs a
TrueType face for the classic Windows console. The words are the same, but the platforms
are opposite. That module uses a Win32 font resource. This module uses a PSF bitmap that
the console driver of the Linux kernel reads.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..core.config import default_config_dir
from ..persistence.logging import get_logger
from ..platforms import get_platform

#: The directory where ``scripts/picocalc-lyra/calculinux-console-font-6x12.sh`` and the
#: related 6x8 script install the fonts.
FONT_DIR = Path("/usr/share/consolefonts")

#: One preference value -> the file that the build script writes for it. This map is next
#: to the code that runs ``setfont``, not in the preference registry. The user selects a
#: cell size and a row count, not a path on a filesystem that only one machine has.
FONT_FILES: dict[str, str] = {"6x12": "meshterm.psf.gz", "6x8": "meshterm8.psf.gz"}

#: The preference that this module uses. It is named one time, so that the caller and the
#: registry agree.
PREFERENCE_KEY = "console_font"

#: The file that :func:`remember` writes in the MeshTerm home. It is a plain ``.psf``,
#: because ``setfont -O`` writes an uncompressed PSF. Also, the file is temporary
#: (:func:`restore` deletes it). Thus it is never gzipped and never has a version.
SAVED_NAME = "console-font-before.psf"

#: A Linux virtual terminal, which is the only type of console that ``setfont`` can
#: communicate with. ``/dev/pts/3`` (an ssh session, or a terminal emulator on the same
#: handheld) is not a VT. If ``setfont`` runs against it, ``setfont`` fails, or it changes
#: a console that nobody looks at.
_VT_NAME = re.compile(r"^/dev/tty[0-9]+$")

#: The seconds before MeshTerm stops a ``setfont`` call. In practice, the call is an ioctl
#: of less than one millisecond. The timeout is here so that a stuck binary cannot block
#: the startup of the app.
_TIMEOUT_S = 5.0


def _controlling_vt() -> str | None:
    """The name of the terminal of stdin when it is a Linux VT, else ``None``.

    ``os.ttyname`` does not exist on Windows, and it raises an error on a redirected
    stdin. For this module, both cases mean "not a VT".
    """
    try:
        name = os.ttyname(0)
    except (AttributeError, OSError, ValueError):
        return None
    return name if _VT_NAME.match(name) else None


def applies() -> bool:
    """Whether this session can change the console font at all.

    There are three conditions. If one of them is false, MeshTerm does not change the
    console, for a different reason each time:

    - The platform must be the PicoCalc, because it is the only platform whose fonts this
      module knows.
    - stdin must be the Linux VT whose font changes (the pty of an ssh session has no font
      of its own).
    - ``setfont`` must exist. On the handheld, this means the ``kbd`` package, which the
      font build must have already.

    Returns:
        ``True`` when all three conditions are true. Otherwise ``False``, with one debug
        line that names the condition that is false. The log is the place that explains a
        font that "did nothing".
    """
    platform = get_platform()
    if platform.name != "picocalc-lyra":
        get_logger().debug("console font: leaving it alone — platform is %s", platform.name)
        return False
    if _controlling_vt() is None:
        get_logger().debug("console font: leaving it alone — stdin is not a Linux VT")
        return False
    if shutil.which("setfont") is None:
        get_logger().debug("console font: leaving it alone — no setfont on PATH")
        return False
    return True


def remember() -> Path | None:
    """Write the current font of the console to a file, and arm its restore.

    The file holds the font of the shell: the font that was loaded before MeshTerm
    started. That font is usually the 6x12 default, but it is the font that the user chose
    last. Thus the restore puts back the font that the user had, not the font that
    MeshTerm thinks that they had.

    This function registers the restore with :mod:`atexit`, and the ``finally`` of the
    session also runs the restore. A full-screen app has many ways to exit (^Q, an
    unhandled exception, the unwind of the menu), and the user sees a console with the
    wrong font only after the app is gone. Two restores do no harm: the first deletes the
    file, and the second finds nothing to do.

    Returns:
        The path of the font file, to give to :func:`restore`. ``None`` when there was
        nothing to remember (refer to :func:`applies`), or when the write failed.
    """
    if not applies():
        return None
    path = default_config_dir() / SAVED_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        get_logger().debug("console font: cannot make %s: %s", path.parent, exc)
        return None
    if not _setfont("-O", str(path)):
        return None
    atexit.register(restore, path)
    return path


def apply(choice: str) -> bool:
    """Load the font that one preference value names.

    Args:
        choice: A :data:`FONT_FILES` key: the value of the ``console_font`` preference.

    Returns:
        Whether ``setfont`` ran and succeeded. ``False`` is the usual answer on each
        machine that is not the handheld. It is never an error that the caller must
        handle.
    """
    if not applies():
        return False
    name = FONT_FILES.get(str(choice))
    if name is None:
        get_logger().debug("console font: no file for %r; leaving the console alone", choice)
        return False
    # ``as_posix``: this path is on the filesystem of the handheld, on each machine that
    # makes the string. Without it, the Windows computer of a developer writes the path
    # with backslashes when a test uses it.
    return _setfont((FONT_DIR / name).as_posix())


def restore(saved: Path | None) -> None:
    """Put back the font that :func:`remember` wrote, then delete the file.

    This function is idempotent by design. It deletes the file when the load worked and
    when it did not. Thus a second call (the ``atexit`` call, after the ``finally`` of the
    session ran) finds nothing and does nothing.

    Args:
        saved: What :func:`remember` returned. ``None`` means there was nothing to do.
    """
    if saved is None:
        return
    try:
        present = saved.is_file()
    except OSError:  # pragma: no cover - if the stat fails, we cannot use the file
        present = False
    if present and shutil.which("setfont") is not None:
        _setfont(str(saved))
    try:
        saved.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - a file we wrote should be ours to remove
        get_logger().debug("console font: cannot remove %s: %s", saved, exc)


def _setfont(*args: str) -> bool:
    """Run ``setfont`` with ``args``, and log all that it says about a failure.

    Never ``-C``: when no tty is named, ``setfont`` acts on its controlling terminal. That
    terminal is the console that the user looks at.

    Returns:
        ``True`` only on a clean exit.
    """
    argv = ["setfont", *args]
    try:
        done = subprocess.run(  # noqa: S603 - a fixed binary name with our own arguments
            argv, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        get_logger().debug("console font: %s failed: %s", " ".join(argv), exc)
        return False
    if done.returncode != 0:
        get_logger().debug(
            "console font: %s exited %d: %s",
            " ".join(argv),
            done.returncode,
            (done.stderr or "").strip() or "(no output)",
        )
        return False
    return True


__all__ = [
    "FONT_DIR",
    "FONT_FILES",
    "PREFERENCE_KEY",
    "SAVED_NAME",
    "applies",
    "apply",
    "remember",
    "restore",
]
