# SPDX-License-Identifier: Apache-2.0
"""Logging setup: a plain text file that you can open, and a console that does not interfere.

There are two sinks. The console handler uses Rich formatting. It shows the records that
are for a person who watches the terminal. The file handler writes ordinary log lines to
``<config_dir>/meshterm.log``: one record on each line, with the timestamp, the level,
the logger, and the message. A traceback is indented under the line that raised it.

The file is plain text on purpose. It was JSON Lines before, because some program might
replay it one day. No program ever did. The only job of the file is this: a person who
has a problem opens it. Often this person reports the problem, and pastes the last twenty
lines into an issue. That person has a text editor, not a JSON parser.

The file rotates. Before, it was a single :class:`~logging.FileHandler`, which grows with
no limit. A real log file got to 11MB in ten weeks. That is a large quantity of disk
space on a handheld, and it is too large to attach to a bug report.

A preference sets how much MeshTerm writes: ``log_level``, with the default ``WARNING``.
Thus the file has the problems, not a full account of a session that works. When you
look for the cause of a problem, set it lower, to ``INFO`` or ``DEBUG``.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_LOGGER_NAME = "meshterm"

#: The log file, in the config directory next to the data that it describes.
LOG_FILENAME = "meshterm.log"

#: The size at which the file rolls over, and the number of rolled files to keep. This is
#: approximately two weeks of a session with much traffic at DEBUG. Also, the full set is
#: small enough to attach to an issue with no problem.
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

#: ``2026-09-07 14:23:11 WARNING  meshterm.core.connection: link lost, retrying``
#: The level is padded, so that the messages align. The logger name tells which part of
#: MeshTerm wrote the record.
_LINE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


#: The levels that the ``log_level`` preference offers, by their own names. They are
#: written out here, instead of taken from ``logging.getLevelName`` or
#: ``getLevelNamesMapping``. The string-to-number direction of ``getLevelName`` is
#: deprecated. ``getLevelNamesMapping`` came in Python 3.11, and this package supports 3.10.
_LEVELS: dict[str, int] = {
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def level_from_name(name: str, *, fallback: int = logging.WARNING) -> int:
    """Change a value of the ``log_level`` preference into a level number.

    Args:
        name: A level name, in any letter case.
        fallback: The level to use when the name is not known. A typo in a
            ``preferences.toml`` that a person edited must cause less log detail. It must
            not prevent the start of the app.

    Returns:
        The level number that matches.
    """
    return _LEVELS.get(name.strip().upper(), fallback)


def log_path(log_dir: Path) -> Path:
    """Return the path of the log file, for an error message that names it."""
    return log_dir / LOG_FILENAME


def log_file_for(level: int) -> Path | None:
    """Return the file that gets a record at ``level``, or ``None`` if no file gets it.

    This function is for a message that says "the full error is in …". The message can say
    this only when the record went to that file, and the ``log_level`` preference controls
    that. If a user set the level of the file to ``ERROR``, the file has no warnings. Thus
    the message must not tell the user to look for them there.

    Args:
        level: The level at which the record was logged.

    Returns:
        The path of the log file, or ``None`` when logging is not configured or the level
        of the file is above ``level``.
    """
    for handler in logging.getLogger(_LOGGER_NAME).handlers:
        if isinstance(handler, logging.FileHandler) and handler.level <= level:
            return Path(handler.baseFilename)
    return None


def configure_logging(
    console: Console,
    log_dir: Path,
    *,
    level: int = logging.INFO,
    file_level: int = logging.WARNING,
    quiet: bool = False,
) -> logging.Logger:
    """Configure and return the application logger.

    Args:
        console: The Rich console to which the handler renders.
        log_dir: The directory for the ``meshterm.log`` file.
        level: The log level of the console.
        file_level: The log level of the file, from the ``log_level`` preference.
        quiet: When ``True``, write nothing to the console. Logging to the file continues.

    Returns:
        The configured ``meshterm`` logger.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    _drop_handlers(logger)
    logger.propagate = False

    if not quiet:
        # markup=False: a log line can have any text that the device sent (a node name, a
        # firmware error). With markup on, Rich reads `[...]` in such text as a style tag.
        # Because of this, the error path was the path most likely to fail. A report of a
        # fault on a node with the name `[/]Bob` raised MarkupError in the handler, and a
        # traceback about the diagnostic replaced the diagnostic. No log call in the
        # package uses markup in its message, so to parse the messages gives no benefit.
        rich_handler = RichHandler(
            console=console, rich_tracebacks=True, show_path=False, markup=False
        )
        rich_handler.setLevel(level)
        logger.addHandler(rich_handler)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path(log_dir),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(_LINE_FORMAT, datefmt=_TIME_FORMAT))
    logger.addHandler(file_handler)

    _quiet_library_console(file_handler)

    return logger


#: Third-party loggers that must never write to the terminal, because their records print
#: over the full-screen TUI. This module routes them to the file handler instead, so that
#: the records stay available for debugging. ``meshcore`` is the main cause: it calls
#: ``logging.basicConfig(level=INFO)`` when it is imported (for example,
#: "INFO:meshcore:Serial Connection started"). ``asyncio`` is in the list so that its
#: default exception handler logs to the file instead of the console. That handler reports
#: stray errors of background tasks after the prompt_toolkit handler that dumps errors on
#: the screen is turned off.
_LIBRARY_LOGGERS: tuple[str, ...] = ("meshcore", "asyncio")


def _quiet_library_console(file_handler: logging.Handler) -> None:
    """Keep the output of noisy third-party libraries off the terminal (it corrupts the TUI).

    The ``meshcore`` client calls ``logging.basicConfig(level=INFO)`` when it is imported.
    This call installs a stream handler on the root logger, and that handler prints its
    INFO lines over the screen. Two guards prevent this problem:

    1. Give the root logger a :class:`~logging.NullHandler`. ``basicConfig`` acts only
       when the root has no handlers. Thus it becomes a no-op and never adds its stream
       handler.
    2. Turn off propagation for each known library logger, and attach only
       ``file_handler``. Thus its records still go to the log, but they never get to the
       console, also if some other code path installs a stream handler on the root logger.

    Args:
        file_handler: The file handler of the application. It also gets the library
            records.
    """
    root = logging.getLogger()
    if not any(isinstance(h, logging.NullHandler) for h in root.handlers):
        root.addHandler(logging.NullHandler())

    for name in _LIBRARY_LOGGERS:
        lib = logging.getLogger(name)
        _drop_handlers(lib)
        lib.setLevel(logging.INFO)
        lib.propagate = False
        lib.addHandler(file_handler)


def _drop_handlers(logger: logging.Logger) -> None:
    """Detach the handlers of ``logger``, and close each one.

    If the function only clears the list, it removes the reference but leaves the file
    open. Each new configuration (a reconnect in the app, or each in-process CLI run under
    test) leaked one more descriptor on ``meshterm.log``. On Windows, such a descriptor is
    also a lock on a file that the rotation must rename later. A handler that fails to
    close is removed all the same, because the purpose of this call is that the old
    handlers get no more records.
    """
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - a handler that we discard must not stop the app
            pass
    logger.handlers.clear()


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the application logger.

    Args:
        name: An optional child name. When it is ``None``, the function returns the root
            logger of the app.

    Returns:
        The requested logger.
    """
    if name is None:
        return logging.getLogger(_LOGGER_NAME)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
