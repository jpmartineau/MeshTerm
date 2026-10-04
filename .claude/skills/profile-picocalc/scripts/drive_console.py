"""Drive MeshTerm on the PicoCalc's own console and time each keystroke end to end.

Runs the app inside a pty sized to the panel, mirrors every byte it writes to /dev/tty1 so
the screen shows exactly what a person sitting in front of it would see, and injects
keystrokes from a script. For each key it reports the time from "key delivered" to "the
app stopped writing" — the whole app-side cost of that keystroke, on the real device,
against the real radio and the real database.

Usage:
    python drive_console.py --script nav.txt [--mock] [--tty /dev/tty1]
                            [--shots DIR] [--tee FILE] [--out results.json]
                            [--exe /path/to/meshterm]

Script lines:  KEY [repeat]      e.g.  "down 5", "enter", "esc", "text:c"
               sleep SECONDS     let the app settle (a screen opening, a radio connect)
               label: NAME       start a new measurement group
               shot NAME         dump the console text right now
               #  comments and blank lines are ignored

Keys: up down left right enter esc tab backspace pgup pgdn home end space f1..f10,
and `text:X` to type a literal character.

--shots reads /dev/vcs1, the console's own screen memory: the exact text on the panel,
which is the only sound way to compare two renderings. Needs membership of the `tty`
group (see the skill's SKILL.md).

Note the app boots to a device picker and does NOT auto-connect — a tour has to press
`enter` on the splash and then `sleep 25` for the radio, or everything after it measures
the splash instead of the screen it names.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time

KEYS = {
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "enter": "\r",
    "esc": "\x1b",
    "tab": "\t",
    "backspace": "\x7f",
    "pgup": "\x1b[5~",
    "pgdn": "\x1b[6~",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "space": " ",
    "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
    "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~",
    "f9": "\x1b[20~", "f10": "\x1b[21~",
}

#: A keystroke is "done" once the app has written nothing for this long. The app writes
#: its frame in one burst, so this cleanly separates one repaint from the next without
#: charging the gap to the keystroke.
#:
#: Raise it (``--quiet-ms``) when measuring something that *opens* a screen rather than
#: repainting one. An open paints the menu closing, then goes quiet for as long as it takes
#: to load and build, then paints the new screen — at the repaint threshold the clock stops
#: on that first burst and reports a fast open that never happened.
QUIET_S = 0.045

#: Give up on a keystroke that never produces output (a key the screen ignores).
TIMEOUT_S = 6.0


def set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--tty", default="/dev/tty1")
    ap.add_argument("--rows", type=int, default=26)
    ap.add_argument("--cols", type=int, default=53)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--boot-wait", type=float, default=12.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--exe", default=os.path.expanduser("~/MeshTerm/.venv/bin/meshterm"))
    ap.add_argument("--quiet-ms", type=float, default=QUIET_S * 1000,
                    help="silence that ends a keystroke; raise it to time screen opens")
    ap.add_argument("--tee", default="", help="also copy the app's output to this file")
    ap.add_argument("--shots", default="", help="dump the console text after each group here")
    args = ap.parse_args()

    tee = open(args.tee, "wb", buffering=0) if args.tee else None
    if args.shots:
        os.makedirs(args.shots, exist_ok=True)

    shot_n = [0]
    last_key = [None]  # when the most recent key was delivered, for `expect`

    def screen_text():
        """The panel's current text, straight out of the console's screen memory."""
        try:
            with open("/dev/vcs1", "rb") as fh:
                return fh.read(args.rows * args.cols).decode("utf-8", "replace")
        except OSError:
            return ""

    def shot(tag):
        """Read the console's own screen memory — exactly what is on the panel."""
        if not args.shots:
            return
        try:
            with open("/dev/vcs1", "rb") as fh:
                data = fh.read(args.rows * args.cols)
        except OSError as exc:
            data = f"<unreadable: {exc}>".encode()
        shot_n[0] += 1
        text = data.decode("utf-8", "replace")
        lines = [text[i * args.cols:(i + 1) * args.cols] for i in range(args.rows)]
        path = os.path.join(args.shots, f"{shot_n[0]:02d}_{tag}.txt")
        with open(path, "w") as fh:
            fh.write("\n".join(lines))

    steps = []
    for raw in open(args.script):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        steps.append(line)

    mirror = open(args.tty, "wb", buffering=0)

    pid, fd = pty.fork()
    if pid == 0:  # child: become the app
        os.environ["TERM"] = "linux"
        os.environ["MESHTERM_FULL_WIDTH"] = "0"
        argv = [args.exe, "--platform", "picocalc-lyra"]
        if args.mock:
            argv.append("--mock")
        os.execv(argv[0], argv)
        os._exit(1)

    set_winsize(fd, args.rows, args.cols)

    quiet_s = args.quiet_ms / 1000.0

    def drain(quiet=None, timeout=TIMEOUT_S, mirror_out=True):
        """Read until the app has been silent for `quiet`; return (bytes, first, last)."""
        if quiet is None:
            quiet = quiet_s
        total = 0
        t_start = time.perf_counter()
        t_first = None
        t_last = t_start
        while True:
            budget = quiet if t_first is not None else timeout
            remaining = budget - (time.perf_counter() - t_last)
            if remaining <= 0:
                break
            r, _, _ = select.select([fd], [], [], remaining)
            if not r:
                break
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            now = time.perf_counter()
            if t_first is None:
                t_first = now
            t_last = now
            total += len(data)
            if mirror_out:
                mirror.write(data)
            if tee is not None:
                tee.write(data)
        return total, t_first, t_last, t_start

    # Boot: let the app come up and connect to the radio.
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < args.boot_wait:
        drain(quiet=0.5, timeout=1.0)

    rows_out = []
    label = "boot"
    print(f"{'label':<20} {'key':<10} {'n':>3} {'p50 ms':>8} {'p90 ms':>8} "
          f"{'max ms':>8} {'bytes':>7}")
    print("-" * 74)

    def flush_group(group, label, key):
        if not group:
            return
        g = sorted(x[0] for x in group)
        p50 = g[len(g) // 2]
        p90 = g[min(len(g) - 1, int(len(g) * 0.9))]
        b = sum(x[1] for x in group) / len(group)
        print(f"{label:<20} {key:<10} {len(g):3d} {p50:8.1f} {p90:8.1f} {g[-1]:8.1f} {b:7.0f}")
        rows_out.append({"label": label, "key": key, "n": len(g), "p50": p50,
                         "p90": p90, "max": g[-1], "bytes": b})

    shot("boot")
    for line in steps:
        if line.startswith("label:"):
            shot(label.replace("/", "-"))
            label = line.split(":", 1)[1].strip()
            continue
        if line.startswith("sleep"):
            time.sleep(float(line.split()[1]))
            drain()
            continue
        if line.startswith("shot"):
            shot(line.split(None, 1)[1].strip() if " " in line else label)
            continue
        if line.startswith("expect "):
            # Time how long until the panel actually shows the screen we asked for.
            # Quiescence cannot answer this: an *open* paints the menu closing, falls
            # silent while it loads and builds, then paints the new screen — and widening
            # the silence window far enough to span that gap lets the 2 s header tick
            # into every other measurement instead. So watch the console itself.
            needle = line[7:].strip()
            deadline = time.perf_counter() + 30.0
            found = None
            while time.perf_counter() < deadline:
                drain(quiet=0.005, timeout=0.005)
                if needle in screen_text():
                    found = time.perf_counter()
                    break
            base = last_key[0] if last_key[0] else time.perf_counter()
            if found is None:
                print(f"{label:<20} {'expect':<10} {'--':>3} "
                      f"{'NEVER APPEARED: ' + needle:>40}")
            else:
                rows_out.append({"label": label, "key": f"expect:{needle}",
                                 "n": 1, "p50": (found - base) * 1000})
                print(f"{label:<20} {'appear':<10} {1:3d} "
                      f"{(found - base) * 1000:8.1f} {'':>8} {'':>8} {'':>7}"
                      f"  <- {needle}")
            continue
        m = re.match(r"^(\S+?)(?:\s+(\d+))?$", line)
        name, count = m.group(1), int(m.group(2) or 1)
        if name.startswith("text:"):
            seq = name.split(":", 1)[1]
            key_name = f"text:{seq}"
        else:
            seq = KEYS.get(name)
            key_name = name
            if seq is None:
                print(f"  ?? unknown key {name!r}", file=sys.stderr)
                continue
        group = []
        for _ in range(count):
            # Drain anything still in flight from the previous key. Without this a slow
            # app's tail lands on the next key's clock and reads as a 0.3 ms response.
            while True:
                left, first, _, _ = drain(timeout=quiet_s, mirror_out=True)
                if left == 0:
                    break
            os.write(fd, seq.encode())
            t_send = time.perf_counter()
            last_key[0] = t_send
            nbytes, t_first, t_last, _ = drain()
            if t_first is None:
                continue
            group.append(((t_last - t_send) * 1000.0, nbytes,
                          (t_first - t_send) * 1000.0))
            time.sleep(0.15)  # let the panel settle; measure keystrokes, not a burst
        flush_group(group, label, key_name)

    shot(label.replace("/", "-"))
    os.write(fd, b"\x03")
    time.sleep(0.4)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    if args.out:
        import json

        with open(args.out, "w") as fh:
            json.dump(rows_out, fh, indent=1)


if __name__ == "__main__":
    main()
