# SPDX-License-Identifier: Apache-2.0
r"""Install and select a console font on Windows, for one user, without admin rights.

The classic Windows console does no font fallback. If its configured font does not have
a character, the console draws a box, and nothing replaces it (refer to
:func:`meshterm.ui.termfont.emoji_support` for the related problem). Its default font is
Consolas. Consolas has 57 of the 122 non-ASCII characters that MeshTerm draws, and none
of the 44 braille characters of the charts. The classic console of Windows PowerShell is
worse: its default font, Lucida Console, does not have even ``●``.

Thus, on that host, MeshTerm offers to correct the font. Two Win32 facts make this
possible without an installer and without administrator rights:

* A font installs **for one user** when you copy it into ``%LOCALAPPDATA%\Microsoft\
  Windows\Fonts``, register it under ``HKCU``, and tell the session about it with
  ``AddFontResourceW`` and a ``WM_FONTCHANGE`` broadcast. No step needs elevation.
* A console selects a font with ``SetCurrentConsoleFontEx``. We checked on Windows 10
  22H2 that this function accepts a face that the properties dialog of the console does
  not list. That dialog offers only the fonts that are registered under the machine-wide
  ``Console\TrueTypeFont`` key (here, Lucida Console and Consolas). Without this API,
  the user can install the font but cannot select it.

**A font install cannot be undone in a session.** After Windows loads the font, it locks
the file. Even an elevated process cannot delete the file, also after a restart of the
font cache service. Windows releases the file at the next logon. Thus nothing here
offers to uninstall a font. We wrote the install so that it is safe to repeat, not so
that it can be reversed.

Each step is best-effort. If a step fails, the app continues with the font that it
already had. That is the same state as without this module.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import win32dll

#: Where the bundled font is. MeshTerm finds it in the same way as each other asset
#: (refer to :mod:`meshterm.ui.about`), so that the path stays correct inside a
#: PyInstaller bundle.
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

#: The font that MeshTerm ships and offers to install. Cascadia Mono PL is the console
#: font of Microsoft, under the SIL OFL (its licence is next to it). The ``PL`` build adds
#: the powerline separators with which MeshTerm draws the path lines. It is 723KB for 109
#: of our 122 characters, all 44 braille characters, and all 3 powerline glyphs. The
#: 2.4MB Nerd Font build covers no more. Almost all mainstream fonts for code (Hack,
#: JetBrains Mono, Fira Code, Source Code Pro, and also the Mono variant of DejaVu Sans)
#: have no braille. Cascadia and Iosevka are the exceptions. Thus this choice is not a
#: matter of taste.
BUNDLED_FONT = FONT_DIR / "CascadiaMonoPL.ttf"

#: The family name of the font, as the ``name`` table spells it.
#: ``SetCurrentConsoleFontEx`` and the ``HKCU`` registration must both get exactly this
#: name.
BUNDLED_FACE = "Cascadia Mono PL"

_USER_FONTS = "Microsoft/Windows/Fonts"
_FONT_REGISTRY = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"

_HWND_BROADCAST = 0xFFFF
_WM_FONTCHANGE = 0x001D
_SMTO_ABORTIFHUNG = 0x0002

#: ``FF_MODERN | TMPF_VECTOR | TMPF_TRUETYPE``: what a console expects a TrueType face to
#: declare. If the value here is 0, the call succeeds, but it quietly keeps the old raster
#: font.
_FF_MODERN_TRUETYPE = 54


def user_font_dir() -> Path:
    r"""The font folder for one user.

    Unlike ``C:\Windows\Fonts``, this folder is writable without admin rights.
    """
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / _USER_FONTS


def install_bundled_font() -> bool:
    """Install the bundled font for this user, and tell the session about it.

    It is safe to repeat. If a copy is already installed, the function does not overwrite
    it, for two reasons: Windows locks the file after it loads it, and an overwrite gives
    no benefit.

    Returns:
        ``True`` when the font is installed and usable (also when it was installed
        before).
    """
    if sys.platform != "win32" or not BUNDLED_FONT.is_file():
        return False
    try:
        import ctypes
        import winreg

        target = user_font_dir() / BUNDLED_FONT.name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BUNDLED_FONT, target)

        if not _add_font_resource(target):
            return False

        # The registration stays after a logout. AddFontResourceW alone lasts only for
        # this session. An entry for one user holds the full path, and a machine-wide
        # entry holds a bare filename. This is the key for one user, so the path goes in.
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _FONT_REGISTRY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, f"{BUNDLED_FACE} (TrueType)", 0, winreg.REG_SZ, str(target))

        # Tell all the running programs that the font list changed. The call has a
        # timeout, and it can stop at a hung window, because a broadcast that waits for
        # each top-level window can easily block a startup path.
        user32 = win32dll.user32()
        user32.SendMessageTimeoutW(
            _HWND_BROADCAST,
            _WM_FONTCHANGE,
            0,
            0,
            _SMTO_ABORTIFHUNG,
            1000,
            ctypes.byref(ctypes.c_ulong()),
        )
        return True
    except Exception:  # pragma: no cover - an install that fails leaves the old font
        return False


def _add_font_resource(path: Path) -> bool:
    """Make a font file usable by this process, without an install.

    ``AddFontResourceW`` adds the font to the font table of the running process. The
    ``HKCU`` registration next to it makes the font permanent, and Windows reads that
    registration at the next logon. This function exists for the case between those two
    facts. A second start of MeshTerm in the same session finds its own registration, and
    believes that the font is ready. But nothing loaded the font into this new process yet.

    Args:
        path: The font file, in any location.

    Returns:
        Whether at least one face was added.
    """
    if sys.platform != "win32" or not path.is_file():
        return False
    try:
        import ctypes

        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        return bool(gdi32.AddFontResourceW(ctypes.c_wchar_p(str(path))))
    except Exception:  # pragma: no cover - best effort, like everything else here
        return False


def use(face: str) -> bool:
    """Draw this console with ``face``. If ``face`` is our own copy, load it first.

    The plain :func:`select` is sufficient for a font that the system installed: the
    Cascadia that comes with Windows 11 or with Windows Terminal. It is not sufficient for
    the copy that MeshTerm installed in an earlier run in the same session. That copy is
    registered but not loaded yet (refer to :func:`_add_font_resource`). When you select
    it, the console silently keeps the old font. Thus, if the console refuses the face,
    the function loads the font and tries one more time.

    Args:
        face: The family to draw with.

    Returns:
        Whether the console now draws with ``face``.
    """
    if select(face):
        return True
    if face == BUNDLED_FACE and _add_font_resource(user_font_dir() / BUNDLED_FONT.name):
        return select(face)
    return False


def current_face() -> str | None:
    """The face with which this console draws, or ``None`` when there is no console."""
    return _console_font(None)


def select(face: str) -> bool:
    """Set this console to ``face``, and confirm that the change occurred.

    The call reports success also when the console quietly kept its old font. Thus the
    answer here comes from a read-back, not from the return value.

    Args:
        face: The family name, spelled as the ``name`` table of the font spells it.

    Returns:
        Whether the console now draws with ``face``.
    """
    return _console_font(face) == face


def _console_font(face: str | None) -> str | None:
    """Read the font of the console. When ``face`` is given, set the font to it first.

    One function does both, because the struct, the handle, and the failure modes are the
    same. Also, a set without a read-back proves nothing.

    Args:
        face: The family to select, or ``None`` to only read.

    Returns:
        The face with which the console draws after the call, or ``None`` if the
        function could not ask (no console, not Windows, a failed call).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class _CONSOLE_FONT_INFOEX(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.ULONG),
                ("nFont", wintypes.DWORD),
                ("dwFontSize", _COORD),
                ("FontFamily", wintypes.UINT),
                ("FontWeight", wintypes.UINT),
                ("FaceName", ctypes.c_wchar * 32),
            ]

        kernel32 = win32dll.kernel32()
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        for name in ("GetCurrentConsoleFontEx", "SetCurrentConsoleFontEx"):
            fn = getattr(kernel32, name)
            fn.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(_CONSOLE_FONT_INFOEX)]
            fn.restype = wintypes.BOOL

        handle = kernel32.GetStdHandle(wintypes.DWORD(-11).value)  # STD_OUTPUT_HANDLE
        info = _CONSOLE_FONT_INFOEX()
        info.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
        if not kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(info)):
            return None

        if face is not None:
            # Keep the height that the user chose, and let the width follow the face. A
            # user selects a console font to be legible at a size. The user did not agree
            # to a change of that size.
            info.FaceName = face[:31]
            info.FontFamily = _FF_MODERN_TRUETYPE
            info.dwFontSize = _COORD(0, info.dwFontSize.Y or 16)
            info.FontWeight = 400
            kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(info))
            after = _CONSOLE_FONT_INFOEX()
            after.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
            if not kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(after)):
                return None
            return after.FaceName or None
        return info.FaceName or None
    except Exception:  # pragma: no cover - a probe that fails is an unknown, not a crash
        return None
