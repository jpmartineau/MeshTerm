# SPDX-License-Identifier: Apache-2.0
"""The PicoCalc's console font: remember what was there, switch, put it back.

The handheld draws to a Linux virtual terminal, and a VT loads exactly one font for the
whole console — there is no per-application font the way a desktop terminal emulator has
one. So MeshTerm asking for its 6x8 build (53x40, fourteen more rows) means changing the
font the *shell* is using, and leaving it changed would be MeshTerm walking off with the
reader's console. Hence the shape of this module: **remember** the font the shell had,
**apply** the one the preference names, and **restore** on every way out.

``setfont -O`` writes the current font out with its unicode table, and feeding that file
straight back to ``setfont`` reproduces it exactly — verified on the device. No ``sudo``
is involved: the VT is the process's controlling terminal and is owned by the logged-in
user, which is the whole reason this can be a preference rather than a setup step.
``-C`` is deliberately never passed, so every call targets that controlling VT and not
whichever tty number happens to be first.

Everything here is cosmetic and everything here fails quietly. A missing ``setfont``, a
read-only state directory, an ssh session that is not a VT at all, a non-zero exit — each
is one debug line and a no-op return, because a font is never a reason for the app not to
start. :func:`applies` is the single gate all three verbs ask first, so "this is not that
machine" is answered in one place.

Distinct from :mod:`meshterm.core.consolefont`, which installs a *TrueType* face for the
classic Windows console. Same words, opposite platforms: that one is a Win32 font
resource, this one is a PSF bitmap the Linux kernel's console driver reads.
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

#: Where ``scripts/picocalc-lyra/calculinux-console-font-6x12.sh`` and its 6x8 companion install
#: the fonts.
FONT_DIR = Path("/usr/share/consolefonts")

#: One preference value -> the file the build script writes for it. This map lives beside
#: the code that runs ``setfont`` rather than in the preference registry: a reader picks a
#: cell size and a row count, not a path on a filesystem only one machine has.
FONT_FILES: dict[str, str] = {"6x12": "meshterm.psf.gz", "6x8": "meshterm8.psf.gz"}

#: The preference this module acts on, named once so the caller and the registry agree.
PREFERENCE_KEY = "console_font"

#: What :func:`remember` writes under the MeshTerm home. A plain ``.psf``: ``setfont -O``
#: writes an uncompressed PSF, and the file is transient anyway (:func:`restore` deletes
#: it), so it is never gzipped and never versioned.
SAVED_NAME = "console-font-before.psf"

#: A Linux virtual terminal, which is the only kind of console ``setfont`` can talk to.
#: ``/dev/pts/3`` (an ssh session or a terminal emulator on the same device) is not one,
#: and ``setfont`` against it would either fail or change a console nobody is looking at.
_VT_NAME = re.compile(r"^/dev/tty[0-9]+$")

#: Seconds before a ``setfont`` call is given up on. It is a sub-millisecond ioctl in
#: practice; the timeout is here so a wedged binary cannot hold the app's startup.
_TIMEOUT_S = 5.0


def _controlling_vt() -> str | None:
    """The name of stdin's terminal when it is a Linux VT, else ``None``.

    ``os.ttyname`` does not exist on Windows and raises on a redirected stdin, which are
    both simply "not a VT" as far as this module is concerned.
    """
    try:
        name = os.ttyname(0)
    except (AttributeError, OSError, ValueError):
        return None
    return name if _VT_NAME.match(name) else None


def applies() -> bool:
    """Whether switching the console font is a thing this session can do at all.

    Three conditions, each a different reason to leave the console alone: the PicoCalc is
    the only platform whose fonts this module knows; stdin must be the Linux VT whose font
    would change (an ssh session's pty has no font of its own); and ``setfont`` must
    exist, which on the device means the ``kbd`` package the font build already needs.

    Returns:
        ``True`` when all three hold. Otherwise ``False``, with one debug line naming
        which one did not — the log is where a font that "did nothing" gets explained.
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
    """Save the console's current font, and arm its restore.

    The saved file is the *shell's* font — whatever was loaded before MeshTerm started,
    which is usually the 6x12 default but is whatever the reader last chose — so the
    restore puts back what they had rather than what we assume they had.

    The restore is registered with :mod:`atexit` here as well as being run from the
    session's ``finally``, because the ways out of a full-screen app are many (^Q, an
    unhandled exception, the menu's own unwind) and a console left in the wrong font is
    only noticed after the app is gone. Restoring twice is harmless: the first one deletes
    the file and the second finds nothing to do.

    Returns:
        The path the font was written to, to hand back to :func:`restore`, or ``None``
        when there was nothing to remember (see :func:`applies`) or the write failed.
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
    """Load the font one preference value names.

    Args:
        choice: A :data:`FONT_FILES` key — the ``console_font`` preference's value.

    Returns:
        Whether ``setfont`` ran and succeeded. ``False`` is the ordinary answer on every
        machine that is not the handheld, and never an error the caller has to handle.
    """
    if not applies():
        return False
    name = FONT_FILES.get(str(choice))
    if name is None:
        get_logger().debug("console font: no file for %r; leaving the console alone", choice)
        return False
    # ``as_posix``: this is a path on the handheld's filesystem whatever machine the
    # string is built on, and a developer's Windows box would otherwise spell it with
    # backslashes on its way into a test.
    return _setfont((FONT_DIR / name).as_posix())


def restore(saved: Path | None) -> None:
    """Put back the font :func:`remember` saved, then drop the file.

    Idempotent by construction — the file is deleted whether or not loading it worked, so
    a second call (the ``atexit`` one, after the session's ``finally`` already ran) finds
    nothing and does nothing.

    Args:
        saved: What :func:`remember` returned. ``None`` means there was nothing to do.
    """
    if saved is None:
        return
    try:
        present = saved.is_file()
    except OSError:  # pragma: no cover - a stat that fails is a file we can't use
        present = False
    if present and shutil.which("setfont") is not None:
        _setfont(str(saved))
    try:
        saved.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - a file we wrote should be ours to remove
        get_logger().debug("console font: cannot remove %s: %s", saved, exc)


def _setfont(*args: str) -> bool:
    """Run ``setfont`` with ``args``, logging anything it has to say about failing.

    Never ``-C``: with no tty named, ``setfont`` acts on its controlling terminal, which
    is the console the reader is actually looking at.

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
