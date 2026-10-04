# SPDX-License-Identifier: Apache-2.0
"""``python -m meshterm.emulator`` — run MeshTerm in the Cardputer Zero's emulator.

    python -m meshterm.emulator [--sim [--scale N] | --device] [MESHTERM ARGS...]

``--sim`` (the default off the device) opens the desktop simulator; ``--device`` draws to
the framebuffer and reads the keyboard, and is the default wherever the launcher has named
a framebuffer (``APPLAUNCH_LINUX_FBDEV_DEVICE``). Everything else is passed to MeshTerm as
it would be on a command line — ``--mock`` for the simulated mesh, ``--ble``/``--port``/
``--tcp`` for a companion — with ``--platform cardputer-zero`` supplied by the host.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    """Parse the emulator's own flags, pass the rest to MeshTerm, and run it."""
    from .framebuffer import FB_ENV
    from .run import run

    device = bool(os.environ.get(FB_ENV))
    scale = 3
    rest: list[str] = []
    args = iter(argv)
    for arg in args:
        if arg == "--sim":
            device = False
        elif arg == "--device":
            device = True
        elif arg == "--scale":
            scale = max(1, int(next(args, "3")))
        else:
            rest.append(arg)
    if device:
        from .framebuffer import front_end
    else:
        from .window import front_end as sim_front_end

        def front_end():
            return sim_front_end(scale)

    try:
        build = front_end()
    except FileNotFoundError as missing:
        print(f"meshterm emulator: {missing}", file=sys.stderr)
        return 1
    return run(rest, build)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
