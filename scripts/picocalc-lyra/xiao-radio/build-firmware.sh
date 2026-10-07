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
#   3. builds that environment
#   4. converts the .hex to .uf2
#
# The patch has the form that upstream needs, and we submitted it to MeshCore. After it is
# merged, delete the `git apply` step below. The env is then part of the firmware, and
# nothing needs a patch.
#
# Prerequisites: git, and the PlatformIO CLI on PATH (`pio`). Install pio with:
#     pip install platformio
#
# Usage:
#     sh build-firmware.sh                 # clone+build into ./_meshcore-build
#     MESHCORE_DIR=/path/to/MeshCore sh build-firmware.sh   # use an existing checkout
#
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PATCH="$HERE/meshcore-uart1.patch"
MC="${MESHCORE_DIR:-$HERE/_meshcore-build}"
ENV=Xiao_nrf52_companion_radio_serial
# The commit that this patch is made against (a `dev` commit, because `dev` is the branch
# where MeshCore takes new work). A newer MeshCore can need the edits to be applied again by
# hand (refer to the README, "Updating"). Set MESHCORE_COMMIT= to follow upstream.
PIN="${MESHCORE_COMMIT:-e9edfc8e}"

command -v git >/dev/null 2>&1 || { echo "ERROR: git not found"; exit 1; }
command -v pio >/dev/null 2>&1 || command -v platformio >/dev/null 2>&1 || {
    echo "ERROR: PlatformIO CLI (pio) not found. Install with: pip install platformio"; exit 1; }
PIO=$(command -v pio 2>/dev/null || command -v platformio)

if [ ! -d "$MC/.git" ]; then
    echo ">> cloning MeshCore into $MC"
    git clone https://github.com/meshcore-dev/MeshCore.git "$MC"
fi

echo ">> checkout $PIN + apply patch"
git -C "$MC" fetch --quiet --tags origin 2>/dev/null || true
git -C "$MC" checkout -f "$PIN" 2>/dev/null || {
    echo "   (pinned commit not found; using current checkout)"; }
git -C "$MC" apply "$PATCH"
echo "   applied meshcore-uart1.patch"

echo ">> building $ENV (first build downloads the nRF52 toolchain; be patient)"
( cd "$MC" && "$PIO" run -e "$ENV" )

HEX="$MC/.pio/build/$ENV/firmware.hex"
UF2CONV="$MC/bin/uf2conv/uf2conv.py"
OUT="$HERE/meshcore-xiao-radio.uf2"
echo ">> converting to uf2"
python "$UF2CONV" "$HEX" -c -f 0xADA52840 -o "$OUT"

echo ""
echo "DONE.  Firmware: $OUT"
echo "Next: put the XIAO in bootloader (double-tap reset) and run:  python flash.py"
