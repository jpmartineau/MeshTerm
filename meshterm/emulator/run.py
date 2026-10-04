# SPDX-License-Identifier: Apache-2.0
"""Run MeshTerm inside the emulator: the TUI unchanged, its terminal ours.

:func:`run` builds the three pieces every front end shares — the :class:`~.vt.Terminal` the
TUI's bytes land in, a :class:`HostOutput` prompt_toolkit writes to, and a pipe input the
the keys go into — sets them as prompt_toolkit's app session, and calls the ordinary
CLI with ``--platform cardputer-zero`` exactly as a shell would. A *front end* is the part that
differs: where the pixels go and where the keys come from. It is handed the terminal, the
lock that guards it and a way to type, starts whatever threads it needs, and is told each
time a frame is complete.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Protocol

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from ..platforms import CARDPUTER_ZERO
from .vt import RGB, Terminal


def _hex(colour: str) -> RGB:
    raw = colour.removeprefix("#")
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _palette() -> tuple[RGB, ...]:
    """The 16 named colours: the same RGBs the PicoCalc's console is programmed with."""
    from ..ui.theme import _VT_SLOTS

    return tuple(_hex(rgb) for _, _, rgb in _VT_SLOTS)


#: The ink of a cell whose style names none — a desktop terminal's default foreground,
#: which is what the truecolor theme this platform runs was designed against.
DEFAULT_FG: RGB = (204, 204, 204)
#: The paper of a cell whose style names none.
DEFAULT_BG: RGB = (0, 0, 0)


class _Sink:
    """The text stream a :class:`Vt100_Output` writes to: bytes into the terminal."""

    encoding = "utf-8"

    def __init__(self, terminal: Terminal, lock: threading.Lock, on_frame: Callable[[], None]):
        self._terminal = terminal
        self._lock = lock
        self._on_frame = on_frame
        self._pending: list[str] = []

    def write(self, data: str) -> int:
        self._pending.append(data)
        return len(data)

    def flush(self) -> None:
        if not self._pending:
            return
        data = "".join(self._pending)
        self._pending.clear()
        with self._lock:
            self._terminal.feed(data)
        self._on_frame()

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError("the emulator's output has no file descriptor")


def panel_output(
    terminal: Terminal, lock: threading.Lock, on_frame: Callable[[], None]
) -> Vt100_Output:
    """A prompt_toolkit output drawing into ``terminal``: fixed size, 24-bit colour."""
    size = Size(rows=terminal.rows, columns=terminal.cols)
    return Vt100_Output(
        _Sink(terminal, lock, on_frame),  # type: ignore[arg-type]
        lambda: size,
        term="xterm-256color",
        default_color_depth=ColorDepth.TRUE_COLOR,
        enable_cpr=False,
    )


class FrontEnd(Protocol):
    """Where the pixels go and its keys come from."""

    def frame_ready(self) -> None:
        """A complete frame has landed in the terminal; draw it when convenient."""

    def close(self) -> None:
        """The TUI has ended; stop."""


#: Builds a front end from the terminal, its lock, and a function that types into the TUI.
FrontEndFactory = Callable[[Terminal, threading.Lock, Callable[[str], None]], FrontEnd]


def run(argv: Sequence[str], front_end: FrontEndFactory) -> int:
    """Run the MeshTerm CLI with ``argv`` inside the emulator, drawn by ``front_end``.

    Returns:
        The CLI's exit status.
    """
    from .. import cli

    lock = threading.Lock()
    with create_pipe_input() as pipe:
        terminal = Terminal(
            CARDPUTER_ZERO.readable_cols,
            CARDPUTER_ZERO.readable_rows,
            palette=_palette(),
            reply=pipe.send_text,
        )
        end = front_end(terminal, lock, pipe.send_text)
        output = panel_output(terminal, lock, end.frame_ready)
        try:
            with create_app_session(input=pipe, output=output):
                # Standalone, as a shell would run it: the CLI reports its own errors and
                # ends by raising SystemExit with the status.
                cli.app(args=["--platform", "cardputer-zero", *argv], windows_expand_args=False)
            return 0
        except SystemExit as done:
            return done.code if isinstance(done.code, int) else (0 if done.code is None else 1)
        finally:
            end.close()
