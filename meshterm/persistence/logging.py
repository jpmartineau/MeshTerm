# SPDX-License-Identifier: Apache-2.0
"""Logging setup: a plain text file you can open, and a console that stays out of the way.

Two sinks. The console handler is Rich-formatted and shows what a person watching the
terminal should see. The file handler writes ordinary log lines to
``<config_dir>/meshterm.log`` — one record per line, timestamp, level, logger, message,
with a traceback indented under the line that raised it.

Plain text on purpose. The file used to be JSON Lines, on the theory that something might
replay it; nothing ever did, and the one job it actually has is being opened by a person
who has hit a problem — often the person reporting it, who then pastes the last twenty
lines into an issue. That reader has a text editor, not a JSON parser.

The file rotates. It used to be a single :class:`~logging.FileHandler`, which grows
without limit: a real one reached 11MB over ten weeks, which is both a lot of disk on a
handheld and an unreasonable thing to attach to a bug report.

How much is written is a preference — ``log_level``, defaulting to ``WARNING``, so the
file holds the problems rather than a narration of a working session. Turn it down to
``INFO`` or ``DEBUG`` when chasing something.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_LOGGER_NAME = "meshterm"

#: The log file, in the config directory beside the data it describes.
LOG_FILENAME = "meshterm.log"

#: Keep this much before rolling over, and this many rolled files. Roughly a fortnight of
#: a chatty session at DEBUG, and small enough that the whole set can be attached to an
#: issue without apology.
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

#: ``2026-09-07 14:23:11 WARNING  meshterm.core.connection: link lost, retrying``
#: Level is padded so the messages line up; the logger name says which part spoke.
_LINE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


#: The levels the ``log_level`` preference offers, by their own names. Spelled out rather
#: than taken from ``logging.getLevelName``, whose string-to-number direction is deprecated,
#: or ``getLevelNamesMapping``, which arrived in 3.11 and this package supports 3.10.
_LEVELS: dict[str, int] = {
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def level_from_name(name: str, *, fallback: int = logging.WARNING) -> int:
    """Turn a ``log_level`` preference value into a level number.

    Args:
        name: A level name, in any case.
        fallback: Used when the name is unrecognised — a hand-edited ``preferences.toml``
            with a typo in it should lose some log detail, not stop the app from starting.

    Returns:
        The matching level number.
    """
    return _LEVELS.get(name.strip().upper(), fallback)


def log_path(log_dir: Path) -> Path:
    """Return the log file's path, for an error message that wants to name it."""
    return log_dir / LOG_FILENAME


def log_file_for(level: int) -> Path | None:
    """Return the file a record at ``level`` is written to, or ``None`` if none takes it.

    For a message that wants to say "the full error is in …": it may say so only when the
    record really went there, and the ``log_level`` preference decides that — a reader who
    turned the file down to ``ERROR`` has no warnings in it to be sent looking for.

    Args:
        level: The level the record was logged at.

    Returns:
        The log file's path, or ``None`` when logging isn't configured or the file's level
        is above ``level``.
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
        console: The Rich console the handler should render to.
        log_dir: Directory for the ``meshterm.log`` file.
        level: Console log level.
        file_level: File log level, from the ``log_level`` preference.
        quiet: When ``True``, suppress console output (file logging continues).

    Returns:
        The configured ``meshterm`` logger.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    _drop_handlers(logger)
    logger.propagate = False

    if not quiet:
        # markup=False: a log line carries whatever the device said — a node name, a
        # firmware error — and Rich would read `[...]` in it as a style tag. That made the
        # *error path* the one most likely to fail: reporting a fault on a node called
        # `[/]Bob` raised MarkupError from inside the handler, replacing the diagnostic
        # with a traceback about the diagnostic. No log call in the package marks up its
        # message, so parsing them buys nothing.
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


#: Third-party loggers that must never write to the terminal — their records would print
#: straight over the full-screen TUI. Routed to the file handler instead so they stay
#: captured for debugging. ``meshcore`` is the notable offender: it calls
#: ``logging.basicConfig(level=INFO)`` on import (e.g. "INFO:meshcore:Serial Connection
#: started"). ``asyncio`` is included so its default exception handler — the one that
#: reports stray background-task errors once prompt_toolkit's screen-dumping handler is
#: disabled — logs to the file instead of the console.
_LIBRARY_LOGGERS: tuple[str, ...] = ("meshcore", "asyncio")


def _quiet_library_console(file_handler: logging.Handler) -> None:
    """Keep noisy third-party libraries off the terminal (they corrupt the TUI).

    The ``meshcore`` client calls ``logging.basicConfig(level=INFO)`` when it is imported,
    which installs a stream handler on the *root* logger that prints its INFO lines straight
    over the screen. Two guards prevent that:

    1. Give the root logger a :class:`~logging.NullHandler` so ``basicConfig`` — which only
       acts when the root has no handlers — becomes a no-op and never adds its stream handler.
    2. Take each known library logger off propagation and attach only ``file_handler``, so
       its records are still captured to the log but never reach the console, even if some
       other code path installs a root stream handler anyway.

    Args:
        file_handler: The application's file handler to also capture library records to.
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
    """Detach ``logger``'s handlers, closing each on the way out.

    Clearing the list alone drops the *reference* and leaves the file open: every
    reconfigure — a reconnect in the app, each in-process CLI invocation under test — leaked
    another descriptor on ``meshterm.log``, which on Windows is also a lock on a file the
    rotation then wants to rename. A handler that refuses to close is dropped anyway: the
    point of this call is that the old handlers stop receiving records.
    """
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - a handler we are discarding cannot fail the app
            pass
    logger.handlers.clear()


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the application logger.

    Args:
        name: Optional child name; when ``None`` the root app logger is returned.

    Returns:
        The requested logger.
    """
    if name is None:
        return logging.getLogger(_LOGGER_NAME)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
