# SPDX-License-Identifier: Apache-2.0
"""Run MeshTerm inside the emulator: the TUI unchanged, its terminal ours.

:func:`run` builds the three pieces every front end shares — the :class:`~.vt.Terminal` the
TUI's bytes land in, the prompt_toolkit output that writes to it (:func:`panel_output`), and
a pipe input the keys go into — sets them as prompt_toolkit's app session, and calls the
ordinary CLI with ``--platform`` set to the emulated device, exactly as a shell would. A
*front end* is the part that differs: where the pixels go and where the keys come from. It
is handed the terminal, the lock that guards it and a way to type, starts whatever threads
it needs, and is told each time a frame is complete.
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
    """The 16 named colours: the same RGBs the PicoCalc's console is programmed with."""
    from ..ui.theme import _VT_SLOTS

    return tuple(_hex(rgb) for _, _, rgb in _VT_SLOTS)


#: The ink of a cell whose style names none — a desktop terminal's default foreground,
#: which is what the truecolor theme this platform runs was designed against.
DEFAULT_FG: RGB = (204, 204, 204)
#: The paper of a cell whose style names none.
DEFAULT_BG: RGB = (0, 0, 0)


def default_colours(device: EmulatedDevice) -> tuple[RGB, RGB]:
    """The ink and paper of a cell whose style names none, on ``device``.

    A Linux console's are its own palette's slot 7 on slot 0; MeshTerm drawing a panel
    itself uses a desktop terminal's, which its truecolour theme was designed against.
    """
    if device.console:
        palette = _palette()
        return palette[7], palette[0]
    return DEFAULT_FG, DEFAULT_BG


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
    terminal: Terminal,
    lock: threading.Lock,
    on_frame: Callable[[], None],
    depth: ColorDepth = ColorDepth.TRUE_COLOR,
) -> Vt100_Output:
    """A prompt_toolkit output drawing into ``terminal``: fixed size, ``depth`` colours."""
    size = Size(rows=terminal.rows, columns=terminal.cols)
    return Vt100_Output(
        _Sink(terminal, lock, on_frame),  # type: ignore[arg-type]
        lambda: size,
        term="xterm-256color",
        default_color_depth=depth,
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


def run(
    argv: Sequence[str],
    front_end: FrontEndFactory,
    device: EmulatedDevice = CARDPUTER_ZERO_DEVICE,
) -> int:
    """Run the MeshTerm CLI with ``argv`` as it runs on ``device``, drawn by ``front_end``.

    The CLI resolves ``device``'s platform as one MeshTerm draws itself
    (:func:`~meshterm.platforms.drawn_by_meshterm`): a PicoCalc's console is drawn by the
    emulator here, not by the terminal the emulator was started from.

    Returns:
        The CLI's exit status.
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
                # Standalone, as a shell would run it: the CLI reports its own errors and
                # ends by raising SystemExit with the status.
                cli.app(args=["--platform", device.id, *argv], windows_expand_args=False)
            return 0
        except SystemExit as done:
            return done.code if isinstance(done.code, int) else (0 if done.code is None else 1)
        finally:
            end.close()
