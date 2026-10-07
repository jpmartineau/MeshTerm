#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Save the display of a Cardputer Zero as a PNG, from another computer.

The script reads the framebuffer of the display over SSH and writes it as a PNG of the
display's own size (320×170). The image is exactly what the app wrote, pixel for pixel:
there is no window, no frame, and no scale. This is the image that M5Stack's app store asks
for as a screenshot.

    python scripts/cardputer-zero/screenshot.py --host pi@192.168.0.165 menu.png

The script reads the size, the stride, and the depth of the framebuffer from sysfs. Thus it
does not assume the layout. It reads only the framebuffer. It does not press a key, and the
app does not know that a picture was taken.

Necessary: ``ssh`` on this computer, and a Cardputer user in the ``video`` group (the
default user ``pi`` is in this group).
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import zlib
from pathlib import Path

#: The program that runs on the Cardputer: the geometry from sysfs on the first line, then
#: the raw bytes of the visible part of the framebuffer.
REMOTE = r"""
set -e
dev=$1
name=$(basename "$dev")
sys=/sys/class/graphics/$name
size=$(cat "$sys/virtual_size")
stride=$(cat "$sys/stride")
bpp=$(cat "$sys/bits_per_pixel")
height=${size#*,}
echo "$size $stride $bpp"
head -c $((stride * height)) "$dev"
"""


def rgb565_to_rgb(raw: bytes, width: int, height: int, stride: int) -> bytes:
    """Change little-endian RGB565 rows into 24-bit RGB rows of ``width`` pixels."""
    out = bytearray()
    for y in range(height):
        row = raw[y * stride : y * stride + width * 2]
        for (value,) in struct.iter_unpack("<H", row):
            r, g, b = value >> 11, (value >> 5) & 0x3F, value & 0x1F
            # Copy the high bits into the low bits, so that full intensity stays 255.
            out += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
    return bytes(out)


def png(rgb: bytes, width: int, height: int) -> bytes:
    """Encode 24-bit RGB rows as a PNG, with no library."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    """Read the framebuffer over SSH, and write the PNG."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path, help="the PNG file to write")
    parser.add_argument("--host", required=True, help="the Cardputer, as user@host for ssh")
    parser.add_argument("-i", "--identity", help="the private key for ssh")
    parser.add_argument("--fb", default="/dev/fb0", help="the framebuffer (default /dev/fb0)")
    args = parser.parse_args()

    command = ["ssh", "-o", "BatchMode=yes"]
    if args.identity:
        command += ["-i", str(Path(args.identity).expanduser())]
    command += [args.host, "sh", "-s", "--", args.fb]
    done = subprocess.run(command, input=REMOTE.encode(), capture_output=True, check=False)
    if done.returncode != 0:
        sys.stderr.write(done.stderr.decode(errors="replace"))
        return 1
    head, _, raw = done.stdout.partition(b"\n")
    size, stride, bpp = head.decode().split()
    width, height = (int(n) for n in size.split(","))
    if bpp != "16":
        sys.stderr.write(f"screenshot: the framebuffer has {bpp} bits a pixel, not 16\n")
        return 1
    if len(raw) < int(stride) * height:
        sys.stderr.write(f"screenshot: got {len(raw)} bytes, not {int(stride) * height}\n")
        return 1
    rgb = rgb565_to_rgb(raw, width, height, int(stride))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(png(rgb, width, height))
    print(f"wrote {args.output} ({width}x{height})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
