# SPDX-License-Identifier: Apache-2.0
"""``python -m meshterm.emulator``: run MeshTerm as it runs on a handheld.

    python -m meshterm.emulator DEVICE [--scale N] [MESHTERM ARGS...]
    python -m meshterm.emulator --framebuffer [MESHTERM ARGS...]

``DEVICE`` (``cardputer-zero``, ``picocalc-lyra``) opens a window that shows the display of
that handheld. This is the same as ``meshterm emulate DEVICE``, which is the command that
the user runs. On the Cardputer Zero itself, the launcher names a framebuffer
(``APPLAUNCH_LINUX_FBDEV_DEVICE``). There, the emulator draws to that framebuffer and reads
the keyboard instead. ``--framebuffer`` asks for that behaviour on any machine. All the
other arguments go to MeshTerm as on a command line: ``--mock`` for the simulated mesh, and
``--ble``/``--port``/``--tcp`` for a companion. The emulator supplies ``--platform``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable


def start(
    name: str | None,
    argv: list[str],
    *,
    scale: int = 3,
    framebuffer: bool | None = None,
    complain: Callable[[str], None] | None = None,
) -> int:
    """Run MeshTerm in the emulator: the display of ``name`` in a window, or the real display.

    Args:
        name: The id of the handheld to emulate in a window. It is ignored on the
            framebuffer, because that framebuffer is the Cardputer Zero's own.
        argv: The arguments of MeshTerm itself, passed on as a shell passes them.
        scale: The zoom of the window, in whole pixels.
        framebuffer: Draw to the framebuffer of the handheld (``True``) or to a window
            (``False``). ``None`` decides from whether the launcher named a framebuffer.
        complain: Where the sentence goes that says why the emulator cannot start (stderr
            by default).

    Returns:
        The exit status of MeshTerm, or ``1`` when the emulator could not start.

    Raises:
        ValueError: ``name`` is missing, or it names no handheld that the emulator offers.
    """
    from .devices import CARDPUTER_ZERO_DEVICE, device
    from .framebuffer import FB_ENV
    from .run import run

    say = complain or (lambda line: print(f"meshterm emulate: {line}", file=sys.stderr))
    on_panel = bool(os.environ.get(FB_ENV)) if framebuffer is None else framebuffer
    if on_panel:
        from .framebuffer import front_end as panel_front_end

        emulated = CARDPUTER_ZERO_DEVICE
        factory = panel_front_end
    else:
        if not name:
            raise ValueError("name the device to emulate - cardputer-zero or picocalc-lyra")
        emulated = device(name)
        from .window import front_end as window_front_end

        def factory():  # noqa: ANN202 - the window factory. It is deferred to report its errors.
            return window_front_end(emulated, scale)

    try:
        build = factory()
    except (FileNotFoundError, RuntimeError) as why:
        say(str(why))
        return 1
    return run(argv, build, emulated)


def main(argv: list[str]) -> int:
    """Parse the emulator's own flags, pass the rest to MeshTerm, and run it."""
    name: str | None = None
    framebuffer: bool | None = None
    scale = 3
    rest: list[str] = []
    args = iter(argv)
    for arg in args:
        if arg == "--framebuffer":
            framebuffer = True
        elif arg == "--window":
            framebuffer = False
        elif arg == "--scale":
            scale = max(1, int(next(args, "3")))
        elif name is None and not arg.startswith("-") and not rest:
            name = arg
        else:
            rest.append(arg)
    try:
        return start(name, rest, scale=scale, framebuffer=framebuffer)
    except ValueError as why:
        print(f"meshterm emulate: {why}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
