# SPDX-License-Identifier: Apache-2.0
"""Tests for the private Windows DLL handles.

These tests prove that a crash does not return. The crash occurred only in a frozen build on
Windows. This is the worst place for a bug, because no traceback stayed. The same exit that
printed the traceback destroyed the window that showed it.

``ctypes.windll.kernel32`` is a cache that the whole process shares. The emoji-width probe
of MeshTerm declared ``GetConsoleScreenBufferInfo`` with a pointer to its own copy of the
screen-buffer struct. Then prompt_toolkit called the same function object with a pointer to
its own copy. The two copies have the same layout but are different classes. Thus ctypes
refused the call while the app built its screen.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

from meshterm.core import win32dll

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows libraries")


def test_each_handle_is_its_own() -> None:
    """Two callers get two objects, so neither caller can see the declarations of the other."""
    assert win32dll.kernel32() is not win32dll.kernel32()
    assert win32dll.user32() is not win32dll.user32()


def test_the_shared_windll_is_the_thing_being_avoided() -> None:
    """The premise: ``ctypes.windll`` gives the same function object to each caller.

    If this stops being true, the module is not necessary. Until then, this is the reason
    that the module exists, so the test asserts it and does not assume it.
    """
    first = ctypes.windll.kernel32.GetConsoleScreenBufferInfo
    second = ctypes.windll.kernel32.GetConsoleScreenBufferInfo
    assert first is second


def test_declaring_a_signature_does_not_reach_the_shared_one() -> None:
    """The regression itself: no other code can see our declarations.

    Before this module existed, the assignment below changed the signature that the console
    output of prompt_toolkit called later. Then the application stopped at startup with
    ``expected LP__CSBI instance instead of pointer to CONSOLE_SCREEN_BUFFER_INFO``.
    """

    class _MineAlone(ctypes.Structure):
        _fields_ = [("whatever", ctypes.c_int)]

    shared = ctypes.windll.kernel32.GetConsoleScreenBufferInfo
    before = shared.argtypes

    private = win32dll.kernel32()
    private.GetConsoleScreenBufferInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_MineAlone),
    ]

    assert ctypes.windll.kernel32.GetConsoleScreenBufferInfo.argtypes == before
    assert private.GetConsoleScreenBufferInfo is not shared


def test_the_console_probe_leaves_the_shared_signature_alone() -> None:
    """End to end: a real call site that declares the function must not change the shared one.

    The narrow assertion above can pass while a call site still uses ``ctypes.windll``.
    Thus this test runs a real call site. The emoji-width probe that crashed does not exist
    now. But the check for the classic console declares the same ``GetConsoleScreenBufferInfo``
    with a struct of its own. This is the same crash, in a different module.
    """
    from meshterm.ui import termfont

    shared = ctypes.windll.kernel32.GetConsoleScreenBufferInfo
    before = shared.argtypes
    # There is no console under pytest, so this returns None and gives no answer. That is
    # correct: the declarations occur before the first call in both cases, and this is
    # what matters.
    termfont._is_classic_console()  # noqa: SLF001 - this private path is the purpose of the test
    assert ctypes.windll.kernel32.GetConsoleScreenBufferInfo.argtypes == before
