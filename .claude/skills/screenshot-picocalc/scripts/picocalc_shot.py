# SPDX-License-Identifier: Apache-2.0
"""Take a screenshot of the PicoCalc's panel over SSH and save it as a PNG.

The panel shows whatever the Linux framebuffer (`/dev/fb0`) holds, so reading that device
is a pixel-exact screenshot of whatever is on the console: MeshTerm, a login prompt, a
shell. Nothing is installed on the device and nothing running there is disturbed.

The device half is a short Python program sent inline (base64, so no shell quoting can
mangle it). It asks the driver for the visible resolution, the panning offset and where
each colour channel sits in a pixel (`FBIOGET_VSCREENINFO`), reads only the visible
rows, and writes one JSON header line followed by the raw pixels. The desktop half turns
those into an RGB PNG with nothing but the standard library, so any Python 3.8+ runs it.

Connection details come from `.dev.env` at the repo root (or `DEV_*` environment
variables, which win): `DEV_PICOCALC_HOST`, `DEV_PICOCALC_USER`, `DEV_PICOCALC_SSH_KEY`,
and `DEV_PICOCALC_PASSWORD` for the `sudo` fallback when the login user cannot read the
framebuffer itself.

    python picocalc_shot.py                 # -> ~/Downloads/picocalc-2026-10-01-153012.png
    python picocalc_shot.py --scale 1       # native 320x320
    python picocalc_shot.py --out shot.png  # somewhere else

Prints the saved path as its last line of stdout; everything else goes to stderr.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import shlex
import struct
import subprocess
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

# Runs on the device. `%(fb)s` is the framebuffer's name (fb0). The pixel channel layout
# is read from the driver rather than assumed, so a BGR panel or a 32-bit mode comes out
# in the right colours without a flag.
DEVICE = r"""
import fcntl, json, struct, sys
name = %(fb)r
with open("/sys/class/graphics/%%s/stride" %% name) as s:
    stride = int(s.read())
with open("/dev/" + name, "rb") as f:
    var = fcntl.ioctl(f, 0x4600, bytes(160))  # FBIOGET_VSCREENINFO
    xres, yres, _xv, _yv, xoff, yoff, bpp, _gray = struct.unpack_from("8I", var, 0)
    chans = struct.unpack_from("9I", var, 32)  # red, green, blue: offset, length, msb_right
    f.seek(yoff * stride)
    raw = f.read(yres * stride)
size = bpp // 8
left, right = xoff * size, (xoff + xres) * size
rows = b"".join(raw[r * stride + left : r * stride + right] for r in range(yres))
head = {"w": xres, "h": yres, "bpp": bpp,
        "red": chans[0:2], "green": chans[3:5], "blue": chans[6:8]}
out = sys.stdout.buffer
out.write(json.dumps(head).encode() + b"\n")
out.write(rows)
out.flush()
"""


def load_env() -> dict[str, str]:
    """`.dev.env`'s values, overridden by any `DEV_*` variable already in the environment."""
    env: dict[str, str] = {}
    path = REPO / ".dev.env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'\"")
    env.update({k: v for k, v in os.environ.items() if k.startswith("DEV_")})
    return env


def remote_command(fb: str, password: str | None) -> str:
    """The shell line run on the device: read directly if allowed, else through sudo."""
    code = base64.b64encode((DEVICE % {"fb": fb}).encode()).decode()
    boot = f"import base64;exec(base64.b64decode('{code}'))"
    direct = 'python3 -c "$C"'
    if password:
        fallback = f"printf '%s\\n' {shlex.quote(password)} | sudo -S -p '' {direct}"
    else:
        fallback = f"sudo -n {direct}"
    return f"C={shlex.quote(boot)}; if [ -r /dev/{fb} ]; then {direct}; else {fallback}; fi"


def grab(env: dict[str, str], fb: str) -> bytes:
    host = env.get("DEV_PICOCALC_HOST")
    user = env.get("DEV_PICOCALC_USER")
    if not host or not user:
        sys.exit("picocalc_shot: DEV_PICOCALC_HOST and DEV_PICOCALC_USER must be set in .dev.env")
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    key = env.get("DEV_PICOCALC_SSH_KEY")
    if key and Path(os.path.expanduser(key)).exists():
        args += ["-i", os.path.expanduser(key)]
    args += [f"{user}@{host}", remote_command(fb, env.get("DEV_PICOCALC_PASSWORD"))]
    try:
        done = subprocess.run(args, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        sys.exit(f"picocalc_shot: {host} stopped answering mid-capture")
    if done.returncode == 255:
        sys.exit(f"picocalc_shot: can't reach {user}@{host} over SSH — is the PicoCalc on and "
                 f"on Wi-Fi?\n{done.stderr.decode(errors='replace').strip()}")
    if done.returncode != 0:
        sys.exit(f"picocalc_shot: the device side failed (exit {done.returncode}):\n"
                 f"{done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def decode(payload: bytes) -> tuple[int, int, list[bytes]]:
    """Header + raw framebuffer pixels -> (width, height, one 3-byte RGB value per pixel)."""
    header, _, body = payload.partition(b"\n")
    head = json.loads(header)
    w, h, bpp = head["w"], head["h"], head["bpp"]
    size = bpp // 8
    if bpp not in (16, 24, 32) or len(body) != w * h * size:
        sys.exit(f"picocalc_shot: unexpected framebuffer ({w}x{h} at {bpp} bpp, "
                 f"{len(body)} bytes)")
    channels = [head["red"], head["green"], head["blue"]]

    def rgb(value: int) -> bytes:
        out = bytearray(3)
        for i, (offset, length) in enumerate(channels):
            top = (1 << length) - 1
            out[i] = ((value >> offset) & top) * 255 // top if top else 0
        return bytes(out)

    if bpp == 16:
        lut = [rgb(v) for v in range(1 << 16)]
        pixels = [lut[v] for v in struct.unpack(f"<{w * h}H", body)]
    elif bpp == 32:
        pixels = [rgb(v) for v in struct.unpack(f"<{w * h}I", body)]
    else:
        pixels = [rgb(int.from_bytes(body[i:i + 3], "little")) for i in range(0, len(body), 3)]
    return w, h, pixels


def arrange(w: int, h: int, px: list[bytes], rotate: int, scale: int) -> tuple[int, int, list[bytes]]:
    """Rotate clockwise by `rotate` degrees, then scale up by whole pixels. Returns rows."""
    if rotate == 90:
        grid = [[px[(h - 1 - c) * w + r] for c in range(h)] for r in range(w)]
    elif rotate == 180:
        flat = px[::-1]
        grid = [flat[r * w:(r + 1) * w] for r in range(h)]
    elif rotate == 270:
        grid = [[px[c * w + (w - 1 - r)] for c in range(h)] for r in range(w)]
    else:
        grid = [px[r * w:(r + 1) * w] for r in range(h)]
    rows = []
    for row in grid:
        line = b"".join(p * scale for p in row)
        rows.extend([line] * scale)
    return len(grid[0]) * scale, len(grid) * scale, rows


def png(w: int, h: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def downloads() -> Path:
    """The user's real Downloads folder — Windows lets it be moved, so ask the shell."""
    if sys.platform == "win32":
        try:
            import ctypes
            import uuid

            folder = (ctypes.c_ubyte * 16).from_buffer_copy(
                uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le)
            path = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(folder), 0, None, ctypes.byref(path)) == 0:
                found = Path(path.value)
                ctypes.windll.ole32.CoTaskMemFree(path)
                return found
        except (OSError, AttributeError):
            pass
    return Path.home() / "Downloads"


def main() -> None:
    parser = argparse.ArgumentParser(description="Screenshot the PicoCalc's panel over SSH.")
    parser.add_argument("--out", type=Path,
                        help="file or folder to save to (default: your Downloads folder)")
    parser.add_argument("--scale", type=int, default=2,
                        help="whole-pixel upscale, 1 for native size (default 2)")
    parser.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0,
                        help="rotate clockwise, if the framebuffer is mounted sideways")
    parser.add_argument("--fb", default="fb0", help="framebuffer device (default fb0)")
    opts = parser.parse_args()
    if opts.scale < 1:
        parser.error("--scale must be at least 1")

    w, h, pixels = decode(grab(load_env(), opts.fb))
    w, h, rows = arrange(w, h, pixels, opts.rotate, opts.scale)

    name = datetime.datetime.now().strftime("picocalc-%Y-%m-%d-%H%M%S.png")
    target = opts.out or downloads()
    if target.suffix.lower() != ".png":
        target = target / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png(w, h, rows))
    if not any(p != b"\x00\x00\x00" for p in pixels):
        print("picocalc_shot: the frame is entirely black — the console may be blanked",
              file=sys.stderr)
    print(f"{w}x{h} from the panel", file=sys.stderr)
    print(target)


if __name__ == "__main__":
    main()
