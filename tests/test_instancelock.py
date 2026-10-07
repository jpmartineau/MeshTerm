# SPDX-License-Identifier: Apache-2.0
"""Tests for the guard of one interactive session for each directory, and the atomic writer.

Both exist for the same reason. Each copy of MeshTerm on a machine uses one data directory,
unless the user sets another. Thus two copies can run at the same time by accident, not by
choice.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from meshterm.core.atomicwrite import write_atomically
from meshterm.core.instancelock import LOCK_NAME, InstanceBusy, hold_instance_lock

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_lock_is_released_when_the_holder_leaves(tmp_path: Path) -> None:
    """Two sessions in sequence are permitted. The guard stops an overlap, not a reuse."""
    with hold_instance_lock(tmp_path):
        pass
    with hold_instance_lock(tmp_path):
        pass


def test_the_lock_file_lives_beside_the_data(tmp_path: Path) -> None:
    """The lock is in the directory that it guards.

    If the directory does not exist, MeshTerm makes it.
    """
    target = tmp_path / "not-yet"
    with hold_instance_lock(target):
        assert (target / LOCK_NAME).exists()


def test_a_second_process_is_turned_away(tmp_path: Path) -> None:
    """The real test of an OS lock: another process cannot take it.

    A test in one process proves nothing here. A lock that excludes only the thread that
    already holds it passes a test in one process. But it fails in the one case for which
    the lock exists.
    """
    program = (
        "import pathlib, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from meshterm.core.instancelock import hold_instance_lock, InstanceBusy\n"
        "try:\n"
        f"    with hold_instance_lock(pathlib.Path({str(tmp_path)!r})):\n"
        "        print('TAKEN')\n"
        "except InstanceBusy:\n"
        "    print('REFUSED')\n"
    )
    with hold_instance_lock(tmp_path):
        done = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=60
        )
    assert done.stdout.strip() == "REFUSED", done.stderr[-500:]


def test_the_refusal_says_how_to_run_two(tmp_path: Path) -> None:
    """Most of the message is the way to run two copies, because the user needs this next.

    A user who sees this message does something that is reasonable. For example, the user
    tries a downloaded build, or runs a second radio. A message that says no, but does not
    say how, gives only half of the answer.
    """
    message = str(InstanceBusy(tmp_path))
    assert "MESHTERM_HOME" in message
    assert str(tmp_path) in message


def test_a_write_lands_whole_or_not_at_all(tmp_path: Path) -> None:
    """The written file has exactly the requested content, and no temporary file stays."""
    target = tmp_path / "sub" / "contacts.json"
    write_atomically(target, json.dumps({"a": 1}))
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert list(tmp_path.rglob("*.tmp")) == []


def test_the_scratch_file_is_named_for_its_writer(tmp_path: Path, monkeypatch) -> None:
    """Two processes that write the same file must not use the same temporary name.

    The rename is atomic, but the file that the rename moves is not. A fixed ``.tmp`` name
    lets two writers interleave their writes in one temporary file. Then each writer renames
    the mixed content over the real file.
    """
    seen: list[Path] = []
    real = Path.write_text

    def spy(self: Path, *args: object, **kwargs: object) -> int:
        seen.append(self)
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy)
    write_atomically(tmp_path / "x.json", "{}")
    assert seen and str(__import__("os").getpid()) in seen[0].name


def test_a_failed_write_leaves_no_litter(tmp_path: Path, monkeypatch) -> None:
    """A write that raises an error removes its temporary file. It does not leave a partial file."""

    def boom(self: Path, *args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        write_atomically(tmp_path / "y.json", "{}")
    assert list(tmp_path.rglob("*.tmp")) == []
