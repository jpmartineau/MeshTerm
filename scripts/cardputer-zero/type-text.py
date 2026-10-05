#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""type-text: type on a Cardputer Zero's keyboard from another computer.

A long string — an API key, a Wi-Fi password — is miserable to enter on a 46-key keyboard
with sticky modifiers. This sends text over SSH and presses the keys for you: it writes key
events into the Cardputer's own keyboard device, so whatever has the screen (the launcher,
MeshTerm, any of M5's apps) gets them exactly as if they had been typed.

    python scripts/cardputer-zero/type-text.py --host pi@192.168.0.165 -i ~/.ssh/id_ed25519

It asks for the text with your typing hidden: paste it, then press Enter. A secret never
shows on screen, never enters your shell history, and never sits on a command line where
the process list would show it — it travels over SSH on standard input. Piped input works
too (``... | type-text.py``). ``--enter`` presses Enter on the Cardputer afterwards.

What it can type is what the keyboard has keys for: letters, digits, space, and the
symbols on the Sym layer, which go out as the codes M5's keymap gives them. Anything else
is refused before a single key is pressed.

Needs: ``ssh`` on this computer, and a Cardputer user in the ``input`` group (the stock
``pi`` is).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import subprocess
import sys

#: The keyboard's event device, where the launcher says it is.
KEYBOARD = "/dev/input/by-path/platform-3f804000.i2c-event"

#: Key codes (linux/input-event-codes.h) for what the keyboard can type unshifted.
PLAIN = {
    **{ch: 16 + i for i, ch in enumerate("qwertyuiop")},
    **{ch: 30 + i for i, ch in enumerate("asdfghjkl")},
    **{ch: 44 + i for i, ch in enumerate("zxcvbnm")},
    **{ch: 2 + i for i, ch in enumerate("1234567890")},
    " ": 57,
}

#: The Sym layer: the placeholder codes its keys send, with the symbol M5's keymap
#: (``/usr/share/keymaps/tca8418_keypad_m5stack_keymap.map``) turns each into.
SYM = {
    "!": 26, "@": 27, "#": 39, "$": 40, "%": 41, "^": 43, "&": 51, "*": 52,
    "(": 53, ")": 94, "~": 55, "`": 69, "_": 70, "-": 71, "+": 72, "=": 73,
    "[": 74, "]": 75, "{": 76, "}": 77, ";": 79, ":": 80, "'": 81, '"': 82,
    "<": 83, ">": 85, "\\": 86, "|": 89, ",": 90, ".": 91, "/": 92, "?": 93,
}  # fmt: skip

#: Runs on the Cardputer, under its own Python: presses each key, Shift for capitals.
DEVICE = """
import base64, json, struct, sys, time
EV = struct.Struct("llHHi")
KEYS = json.loads(base64.b64decode(sys.argv[1]))
SHIFT, ENTER = 42, 28
text = sys.stdin.read()
board = open(sys.argv[2], "wb", buffering=0)
def emit(code, value):
    board.write(EV.pack(0, 0, 1, code, value) + EV.pack(0, 0, 0, 0, 0))
def tap(code, shift=False):
    if shift:
        emit(SHIFT, 1); time.sleep(0.02)
    emit(code, 1); time.sleep(0.03); emit(code, 0)
    if shift:
        time.sleep(0.02); emit(SHIFT, 0)
    time.sleep(0.09)
for ch in text:
    upper = ch.isalpha() and ch.isupper()
    tap(KEYS[ch.lower() if upper else ch], upper)
if sys.argv[3] == "1":
    tap(ENTER)
print(f"typed {len(text)} characters on the Cardputer", file=sys.stderr)
"""


def keys_for(text: str) -> dict[str, int]:
    """The key for each character ``text`` uses, or exit naming the ones it can't type."""
    table = {**PLAIN, **SYM}
    missing = sorted({ch for ch in text if (ch.lower() if ch.isalpha() else ch) not in table})
    if missing:
        sys.exit(f"type-text: the Cardputer has no key for {' '.join(map(repr, missing))}")
    return table


def read_text(show: bool) -> str:
    """The text to type: piped in, or asked for (hidden unless ``show``)."""
    if not sys.stdin.isatty():
        text = sys.stdin.read()
    elif show:
        text = input("Text to type: ")
    else:
        text = getpass.getpass("Text to type (hidden — paste it, then press Enter): ")
    return text.strip("\r\n")


def main() -> int:
    """Read the text, then type it on the Cardputer over SSH."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--host",
        default=os.environ.get("CARDPUTER_HOST", "pi@cardputer.local"),
        help="user@host of the Cardputer (default: $CARDPUTER_HOST, else pi@cardputer.local)",
    )
    parser.add_argument("-i", dest="identity", help="SSH private key to log in with")
    parser.add_argument("--enter", action="store_true", help="press Enter after the text")
    parser.add_argument("--show", action="store_true", help="show the text as you type it")
    args = parser.parse_args()

    text = read_text(args.show)
    if not text:
        print("type-text: nothing to type", file=sys.stderr)
        return 1
    table = keys_for(text)
    # Both go as base64: the remote shell sees letters and digits only, so no quote in the
    # program or the symbol table (an apostrophe is one of its keys) can break its quoting.
    program = base64.b64encode(DEVICE.encode()).decode()
    keys = base64.b64encode(json.dumps(table).encode()).decode()
    remote = [
        "python3",
        "-c",
        f"'import base64; exec(base64.b64decode(\"{program}\"))'",
        keys,
        KEYBOARD,
        "1" if args.enter else "0",
    ]
    ssh = ["ssh"] + (["-i", os.path.expanduser(args.identity)] if args.identity else [])
    done = subprocess.run([*ssh, args.host, *remote], input=text, text=True, check=False)
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
