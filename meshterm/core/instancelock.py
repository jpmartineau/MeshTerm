# SPDX-License-Identifier: Apache-2.0
"""One interactive MeshTerm for each data directory, and a clear message when you want two.

Each copy of MeshTerm on a machine reads and writes the same ``~/.meshterm``: a checkout,
a downloaded build, or a second instance for a second companion. The type of installation
does not change that. Two copies that run at the same time do not corrupt data (the
database is in WAL mode, and each file is written through a rename). But each copy keeps
the JSON stores in memory and writes them again in full. Thus the second copy that writes
removes the changes of the first copy, and it does not tell the user. A lost contact is
much more difficult to see than a crash.

Thus the interactive session takes an exclusive lock on the directory that it uses. A
second session stops, and its message gives the solution: set ``MESHTERM_HOME``.

The operating system holds the lock. MeshTerm does not write a process id in a file. A
pid file must answer "is that process still alive?". The answer is different on each
platform, and it is wrong after a reboot gives the same number to a new process. Thus
after a crash, a pid file leaves a lock that nobody can explain and that each user must
bypass. The operating system releases its lock when the process ends, however it ends.

One-shot CLI subcommands do not take the lock. They are short, and they mostly read. If
the lock blocks ``meshterm contacts`` because a menu is open in another place, the lock is
an obstacle, not a guard.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["InstanceBusy", "LOCK_NAME", "hold_instance_lock"]

#: The lock file, next to the data that it guards. It is empty: only the lock on it is
#: important.
LOCK_NAME = ".lock"


class InstanceBusy(RuntimeError):
    """Raised when another interactive MeshTerm already holds this data directory."""

    def __init__(self, config_dir: Path) -> None:
        """Make the message, which mostly tells the user how to solve the problem."""
        super().__init__(
            f"Another MeshTerm is already using {config_dir}.\n\n"
            "Two of them sharing one directory will quietly overwrite each other's\n"
            "contacts and settings, so this one stopped instead.\n\n"
            "To run a second one — a downloaded build beside your own, or a second\n"
            "radio — give it a directory of its own:\n\n"
            "    MESHTERM_HOME=~/meshterm-other meshterm\n"
        )
        self.config_dir = config_dir


def _try_lock(handle) -> bool:
    """Take an exclusive, non-blocking lock on an open file (``False`` if it is not free)."""
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextmanager
def hold_instance_lock(config_dir: Path) -> Iterator[None]:
    """Hold ``config_dir`` for this process, and release it however the process ends.

    Args:
        config_dir: The data directory to claim.

    Raises:
        InstanceBusy: Another interactive MeshTerm holds it.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / LOCK_NAME
    handle = open(path, "a+b")  # noqa: SIM115 - closed in the finally below
    try:
        if not _try_lock(handle):
            raise InstanceBusy(config_dir)
        yield
    finally:
        # The close releases the lock on all platforms. The file itself stays. If MeshTerm
        # deletes it, the delete races a second process that has opened the file already
        # and will lock it next. That race gives one directory to two processes.
        handle.close()
