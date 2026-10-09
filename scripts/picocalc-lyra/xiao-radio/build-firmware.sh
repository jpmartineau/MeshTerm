#!/bin/sh
# build-firmware.sh -- build the XIAO nRF52840 MeshCore radio firmware for the PicoCalc.
#
# Run it on your DEVELOPMENT MACHINE (not the Lyra). It makes one .uf2 that you flash with
# flash.py.
#
# What it does:
#   1. clones MeshCore (pinned to a tested commit) if you do not already have it
#   2. applies meshcore-uart1.patch, which has the two changes that make this work:
#        * teach the existing SERIAL_RX companion interface to build on nRF52 (upstream
#          declares a HardwareSerial for ESP32 only, and nRF52 needs Serial1)
#        * add a Xiao_nrf52_companion_radio_serial env: the companion is on D6/D7, and I2C
#          moves off those pads (to internal pins 16/17), so that I2C cannot take them
#   3. applies meshcore-frame-timeout.patch, which makes the link recover by itself: the
#      serial parser drops a frame whose bytes stop coming, so one lost byte cannot make
#      the companion ignore every command after it
#   4. builds that environment
#   5. converts the .hex to .uf2
#
# Each patch has the form that upstream needs. When upstream merges one, remove it from
# PATCHES below. When both are merged, the env is part of the firmware, and nothing needs
# a patch.
#
# Prerequisites: git, and the PlatformIO CLI (`pio`). Install it with the official installer
# script (refer to Step 11 of docs/devices/picocalc-lyra.md). The script finds `pio` on the
# PATH, or else in ~/.platformio/penv, where the installer and the PlatformIO extension of
# VS Code put it. Thus you do not have to change the PATH.
#
# Usage:
#     sh build-firmware.sh                 # clone+build into ./_meshcore-build
#     MESHCORE_DIR=/path/to/MeshCore sh build-firmware.sh   # use an existing checkout
#
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PATCHES="meshcore-uart1.patch meshcore-frame-timeout.patch"
MC="${MESHCORE_DIR:-$HERE/_meshcore-build}"
ENV=Xiao_nrf52_companion_radio_serial
# The commit that the patches are made against (a `dev` commit, because `dev` is the branch
# where MeshCore takes new work). A newer MeshCore can need the edits to be applied again by
# hand (refer to the guide, "Building by hand"). Set MESHCORE_COMMIT= to follow upstream.
PIN="${MESHCORE_COMMIT:-e9edfc8e}"

command -v git >/dev/null 2>&1 || { echo "ERROR: git not found"; exit 1; }
PIO=$(command -v pio 2>/dev/null || command -v platformio 2>/dev/null) || PIO=
if [ -z "$PIO" ]; then
    # The installer does not put pio on the PATH. Look where it does put it: bin/ on Linux
    # and macOS, Scripts/ on Windows.
    for c in "$HOME/.platformio/penv/bin/pio" "$HOME/.platformio/penv/Scripts/pio.exe"; do
        if [ -x "$c" ]; then PIO=$c; break; fi
    done
fi
[ -n "$PIO" ] || {
    echo "ERROR: PlatformIO (pio) not found, on the PATH or in ~/.platformio/penv."
    echo "Install it with the official installer script (Step 11 of the PicoCalc guide)."
    exit 1; }
echo ">> using $PIO"
# The Python that runs uf2conv.py. PlatformIO's own Python comes first, because it is
# always there when pio is. A bare `python` is absent on many Linux systems, and on Windows
# `python3` can be a stub that opens the Microsoft Store.
PY=
for c in "$(dirname -- "$PIO")/python" "$(dirname -- "$PIO")/python.exe"; do
    if [ -x "$c" ]; then PY=$c; break; fi
done
[ -n "$PY" ] || PY=$(command -v python3 2>/dev/null || command -v python)

if [ ! -d "$MC/.git" ]; then
    echo ">> cloning MeshCore into $MC"
    git clone https://github.com/meshcore-dev/MeshCore.git "$MC"
fi

echo ">> checkout $PIN + apply patches"
git -C "$MC" fetch --quiet --tags origin 2>/dev/null || true
git -C "$MC" checkout -f "$PIN" 2>/dev/null || {
    echo "   (pinned commit not found; using current checkout)"; }
for p in $PATCHES; do
    git -C "$MC" apply "$HERE/$p"
    echo "   applied $p"
done

echo ">> building $ENV (first build downloads the nRF52 toolchain; be patient)"
( cd "$MC" && "$PIO" run -e "$ENV" )

HEX="$MC/.pio/build/$ENV/firmware.hex"
UF2CONV="$MC/bin/uf2conv/uf2conv.py"
OUT="$HERE/meshcore-xiao-radio.uf2"
echo ">> converting to uf2"
"$PY" "$UF2CONV" "$HEX" -c -f 0xADA52840 -o "$OUT"

echo ""
echo "DONE.  Firmware: $OUT"
echo "Next: connect the USB-C port of the XIAO to this machine, and run:  python3 flash.py"
echo "      (on Windows: py flash.py)"
