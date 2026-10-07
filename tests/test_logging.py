# SPDX-License-Identifier: Apache-2.0
"""Tests for the log file: plain text, with a size limit, and with the things that went wrong.

The one real job of the log is to be opened by a person who has a problem. This person is
often the one who reports the problem. Each assertion here is about that user.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console

from meshterm.persistence.logging import (
    LOG_FILENAME,
    configure_logging,
    get_logger,
    level_from_name,
    log_path,
)


def _read(log_dir: Path) -> str:
    for handler in get_logger().handlers:
        handler.flush()
    return log_path(log_dir).read_text(encoding="utf-8")


def test_the_log_is_plain_text(tmp_path: Path) -> None:
    """Each record is one readable line: when, how bad, who said it, and what."""
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.WARNING, quiet=True)
    get_logger("core.connection").warning("link lost, retrying")
    line = _read(tmp_path).splitlines()[0]
    assert "WARNING" in line
    assert "meshterm.core.connection" in line
    assert "link lost, retrying" in line
    # A date and a time. "It happened at 14:40" is not useful one week later.
    assert line.startswith("20")


def test_the_file_is_named_plainly(tmp_path: Path) -> None:
    """The name is ``meshterm.log``, an extension that each text editor opens."""
    assert log_path(tmp_path).name == LOG_FILENAME == "meshterm.log"


def test_the_level_keeps_the_quiet_records_out(tmp_path: Path) -> None:
    """At the default level, the file has the problems, not a narration of a run that works.

    This is the reason that the default is WARNING. If the dozen useful lines are under
    twenty thousand dull lines, nobody reads the log.
    """
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.WARNING, quiet=True)
    log = get_logger()
    log.debug("opened a screen")
    log.info("connected to a radio")
    log.warning("something went wrong")
    written = _read(tmp_path)
    assert "something went wrong" in written
    assert "opened a screen" not in written
    assert "connected to a radio" not in written


def test_turning_it_up_lets_everything_through(tmp_path: Path) -> None:
    """DEBUG is for the search for a problem. It keeps what WARNING removes."""
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.DEBUG, quiet=True)
    get_logger().debug("opened a screen")
    assert "opened a screen" in _read(tmp_path)


def test_a_traceback_goes_in_with_the_record(tmp_path: Path) -> None:
    """The traceback of an exception is written under the record that reports it.

    This is the main reason to attach the file to a bug report. The terminal scrolls and
    the user closes it, but this copy stays.
    """
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.WARNING, quiet=True)
    try:
        raise ValueError("the device said something impossible")
    except ValueError:
        get_logger().exception("tool raised")
    written = _read(tmp_path)
    assert "tool raised" in written
    assert "ValueError: the device said something impossible" in written
    assert "Traceback (most recent call last)" in written


def test_the_file_is_bounded(tmp_path: Path) -> None:
    """The file rotates. A log with no size limit reached 11MB in ten weeks on a real install."""
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.WARNING, quiet=True)
    handler = next(h for h in get_logger().handlers if hasattr(h, "maxBytes"))
    assert handler.maxBytes > 0
    assert handler.backupCount > 0


def test_a_level_name_becomes_a_level(tmp_path: Path) -> None:
    """The preference stores a name, but the handler needs a number."""
    assert level_from_name("DEBUG") == logging.DEBUG
    assert level_from_name("warning") == logging.WARNING
    assert level_from_name("  Error ") == logging.ERROR


def test_a_nonsense_level_still_starts_the_app() -> None:
    """A typo in a preferences file that the user edits by hand loses log detail, not the app.

    A user who edits the preferences file by hand and writes ``WARN`` gets a log that is a
    little quieter. The app must not refuse to start.
    """
    assert level_from_name("WARN") == logging.WARNING
    assert level_from_name("") == logging.WARNING
