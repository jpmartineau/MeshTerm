# SPDX-License-Identifier: Apache-2.0
"""Launcher for the frozen builds.

PyInstaller freezes a script, not an entry point for a console script. This file is that
script. It does what the ``meshterm`` command does and nothing more. It is in ``packaging/``
and not in the package, because it exists for the installers and must not be importable.
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    # Windows and macOS start child processes when they run this executable again. Without
    # this call, a frozen app that starts a child process starts a second copy of the whole
    # UI instead. This looks like an app that starts itself again and again.
    multiprocessing.freeze_support()

    from meshterm.cli import main

    sys.exit(main())
