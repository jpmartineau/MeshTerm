# SPDX-License-Identifier: Apache-2.0
"""Run MeshTerm in the emulator: the TUI does not change, and the emulator supplies its terminal.

:func:`run` builds the three parts that all the front ends share:

* the :class:`~.vt.Terminal` that the bytes of the TUI go to,
* the prompt_toolkit output that writes to that terminal (:func:`panel_output`), and
* a pipe input that the key presses go into.

Then :func:`run` sets them as the app session of prompt_toolkit. It calls the ordinary CLI
with ``--platform`` set to the emulated handheld, exactly as a shell does. A *front end* is
the part that is different: where the pixels go, and where the key presses come from. The
front end gets the terminal, the lock that guards it, and a function to type with. It
starts the threads that it uses, and it gets a call each time a frame is complete.
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

from .. import platforms
from .devices import CARDPUTER_ZERO_DEVICE, EmulatedDevice
from .vt import RGB, Terminal


def _hex(colour: str) -> RGB:
    raw = colour.removeprefix("#")
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _palette() -> tuple[RGB, ...]:
    """The 16 named colours: the same RGB values that the PicoCalc's console is set to."""
    from ..ui.theme import _VT_SLOTS

    return tuple(_hex(rgb) for _, _, rgb in _VT_SLOTS)


#: The ink of a cell whose style names no ink: the default foreground of a desktop
#: terminal. The truecolour theme that this platform runs was designed against it.
DEFAULT_FG: RGB = (204, 204, 204)
#: The paper of a cell whose style names no paper.
DEFAULT_BG: RGB = (0, 0, 0)


def default_colours(device: EmulatedDevice) -> tuple[RGB, RGB]:
    """The ink and the paper of a cell whose style names none, on ``device``.

    A Linux console uses slot 7 on slot 0 of its own palette. When MeshTerm draws a
    display itself, it uses the colours of a desktop terminal, because its truecolour
    theme was designed against them.
    """
    if device.console:
        palette = _palette()
        return palette[7], palette[0]
    return DEFAULT_FG, DEFAULT_BG


class _Sink:
    """The text stream that a :class:`Vt100_Output` writes to: bytes into the terminal."""

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
    terminal: Terminal,
    lock: threading.Lock,
    on_frame: Callable[[], None],
    depth: ColorDepth = ColorDepth.TRUE_COLOR,
) -> Vt100_Output:
    """A prompt_toolkit output that draws into ``terminal``: a fixed size, ``depth`` colours."""
    size = Size(rows=terminal.rows, columns=terminal.cols)
    return Vt100_Output(
        _Sink(terminal, lock, on_frame),  # type: ignore[arg-type]
        lambda: size,
        term="xterm-256color",
        default_color_depth=depth,
        enable_cpr=False,
    )


class FrontEnd(Protocol):
    """Where the pixels go, and where the key presses come from."""

    def frame_ready(self) -> None:
        """A complete frame is in the terminal. Draw it when it is convenient."""

    def close(self) -> None:
        """The TUI has ended. Stop."""


#: Builds a front end from the terminal, its lock, and a function that types into the TUI.
FrontEndFactory = Callable[[Terminal, threading.Lock, Callable[[str], None]], FrontEnd]


def run(
    argv: Sequence[str],
    front_end: FrontEndFactory,
    device: EmulatedDevice = CARDPUTER_ZERO_DEVICE,
) -> int:
    """Run the MeshTerm CLI with ``argv`` as it runs on ``device``, drawn by ``front_end``.

    The CLI resolves the platform of ``device`` as a platform that MeshTerm draws itself
    (:func:`~meshterm.platforms.drawn_by_meshterm`). Thus here the emulator draws the
    console of a PicoCalc, not the terminal from which the emulator was started.

    Returns:
        The exit status of the CLI.
    """
    from .. import cli

    lock = threading.Lock()
    with create_pipe_input() as pipe:
        terminal = Terminal(
            device.platform.readable_cols,
            device.platform.readable_rows,
            palette=_palette(),
            reply=pipe.send_text,
        )
        end = front_end(terminal, lock, pipe.send_text)
        output = panel_output(terminal, lock, end.frame_ready, device.color_depth)
        try:
            with create_app_session(input=pipe, output=output), platforms.drawn_by_meshterm():
                # Standalone, as a shell runs it: the CLI reports its own errors. At the
                # end, it raises SystemExit with the status.
                cli.app(args=["--platform", device.id, *argv], windows_expand_args=False)
            return 0
        except SystemExit as done:
            return done.code if isinstance(done.code, int) else (0 if done.code is None else 1)
        finally:
            end.close()
