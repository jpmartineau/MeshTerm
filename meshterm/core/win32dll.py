# SPDX-License-Identifier: Apache-2.0
"""Private handles on the Windows DLLs, because all code in a process shares ``ctypes.windll``.

``ctypes.windll.kernel32`` is a cache. Each caller in the process gets the same ``WinDLL``
object, and each function on it is a cached attribute. Thus
``ctypes.windll.kernel32.GetConsoleScreenBufferInfo`` is one object, which this
application and all the libraries in it share. When code sets ``.argtypes`` on it, the
change is not local. It is a change for the full process, and the last writer wins.

This problem is real. It caused a crash of MeshTerm on Windows:

* ``ui/tui/emoji_width.py`` measured an emoji: it asked the console for its cursor
  position. It declared that ``GetConsoleScreenBufferInfo`` takes a pointer to its own
  copy of the screen-buffer struct.
* Later, prompt_toolkit called the same function object with a pointer to its own copy of
  the same struct. The two copies had the same layout, but a different class.
* ctypes compared the two classes, found that they were not equal, and refused the call:
  ``expected LP__CSBI instance instead of pointer to CONSOLE_SCREEN_BUFFER_INFO``.

The application crashed while it built its screen. On a build that the user started with
a double-click, the traceback and its window disappeared at the same time.

When code makes a ``WinDLL`` directly, it gets a new object with its own function cache.
Thus only the caller that makes declarations on that object can see them. Each call in
this module returns a new object. If two parts of MeshTerm declare the same function
differently, that is the same bug on a small scale. The cost is one small object at a call
site that runs rarely.

This module exists for one rule: **no code in MeshTerm uses ``ctypes.windll``.**
"""

from __future__ import annotations

import ctypes
import sys

__all__ = ["kernel32", "user32"]


def _library(name: str) -> ctypes.WinDLL:
    """Load ``name`` into a private WinDLL.

    Args:
        name: The DLL to load, without its extension.

    Returns:
        A private handle. No other code can see or change its function signatures.

    Raises:
        RuntimeError: If the call is not on Windows, because these libraries exist only on
            Windows.
    """
    if sys.platform != "win32":  # pragma: no cover - the callers all guard on platform
        raise RuntimeError(f"{name} is a Windows library")
    # `use_last_error` keeps GetLastError unchanged for the caller. It is the only way to
    # know "the call failed" from "the call succeeded and returned zero".
    return ctypes.WinDLL(name, use_last_error=True)


def kernel32() -> ctypes.WinDLL:
    """A private handle on ``kernel32``: console, handles, memory."""
    return _library("kernel32")


def user32() -> ctypes.WinDLL:
    """A private handle on ``user32``: clipboard, keyboard state, windows."""
    return _library("user32")
