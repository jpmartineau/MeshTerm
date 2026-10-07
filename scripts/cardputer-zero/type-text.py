#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Type text on the keyboard of a Cardputer Zero from another computer.

A long string, such as an API key or a Wi-Fi password, is difficult to enter on a keyboard
with 46 keys and sticky modifiers. This script sends the text over SSH and presses the keys
for you. It writes key events into the keyboard device of the Cardputer. Thus the program
that has the screen (the launcher, MeshTerm, or any app of M5) gets the keys in the same
way as if a person typed them.

    python scripts/cardputer-zero/type-text.py --host pi@192.168.0.165 -i ~/.ssh/id_ed25519

The script asks for the text and hides what you type. Paste the text, then press Enter. A
secret never shows on the screen. It never enters your shell history. It never appears on a
command line, where the process list can show it, because it goes over SSH on standard
input. Piped input also works (``... | type-text.py``). ``--enter`` presses Enter on the
Cardputer after the text.

The script can type only the characters that the keyboard has keys for: letters, digits,
space, and the symbols on the Sym layer. The symbols go out as the codes that the keymap of
M5 gives them. The script refuses any other character before it presses a key.

Necessary: ``ssh`` on this computer, and a Cardputer user in the ``input`` group (the
default user ``pi`` is in this group).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import subprocess
import sys

#: The event device of the keyboard, in the place that the launcher gives.
KEYBOARD = "/dev/input/by-path/platform-3f804000.i2c-event"

#: The key codes (linux/input-event-codes.h) of the characters that the keyboard types
#: without Shift.
PLAIN = {
    **{ch: 16 + i for i, ch in enumerate("qwertyuiop")},
    **{ch: 30 + i for i, ch in enumerate("asdfghjkl")},
    **{ch: 44 + i for i, ch in enumerate("zxcvbnm")},
    **{ch: 2 + i for i, ch in enumerate("1234567890")},
    " ": 57,
}

#: The Sym layer: the placeholder codes that its keys send. The keymap of M5
#: (``/usr/share/keymaps/tca8418_keypad_m5stack_keymap.map``) changes each code to a symbol.
SYM = {
    "!": 26, "@": 27, "#": 39, "$": 40, "%": 41, "^": 43, "&": 51, "*": 52,
    "(": 53, ")": 94, "~": 55, "`": 69, "_": 70, "-": 71, "+": 72, "=": 73,
    "[": 74, "]": 75, "{": 76, "}": 77, ";": 79, ":": 80, "'": 81, '"': 82,
    "<": 83, ">": 85, "\\": 86, "|": 89, ",": 90, ".": 91, "/": 92, "?": 93,
}  # fmt: skip

#: This program runs on the Cardputer, under its own Python. It presses each key, and Shift
#: for capitals.
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
    """The key for each character that ``text`` uses.

    If a character has no key, the function exits and names the characters.
    """
    table = {**PLAIN, **SYM}
    missing = sorted({ch for ch in text if (ch.lower() if ch.isalpha() else ch) not in table})
    if missing:
        sys.exit(f"type-text: the Cardputer has no key for {' '.join(map(repr, missing))}")
    return table


def read_text(show: bool) -> str:
    """The text to type: from a pipe, or from a prompt.

    The prompt hides the text unless ``show`` is true.
    """
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
    # Both go as base64. The remote shell sees only letters and digits. Thus no quote in the
    # program or in the symbol table (an apostrophe is one of its keys) can break its quoting.
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
