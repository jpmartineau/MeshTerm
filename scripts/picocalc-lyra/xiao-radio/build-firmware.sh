#!/bin/sh
# build-firmware.sh -- build the XIAO nRF52840 MeshCore radio firmware for the PicoCalc.
#
# Runs on your DEV MACHINE (not the Lyra). Produces a single .uf2 you flash with flash.py.
#
# What it does:
#   1. clones MeshCore (pinned to a tested commit) if you don't already have it
#   2. applies meshcore-uart1.patch -- the two changes that make this work:
#        * teach the existing SERIAL_RX companion interface to build on nRF52 (upstream
#          declares an ESP32-only HardwareSerial; nRF52 wants Serial1)
#        * add a Xiao_nrf52_companion_radio_serial env: companion on D6/D7, and I2C moved
#          off those pads (to internal pins 16/17) so it can't seize them
#   3. builds that environment
#   4. converts the .hex to .uf2
#
# The patch is upstream-shaped and has been submitted to MeshCore. Once it lands, delete
# the `git apply` step below -- the env ships with the firmware and nothing needs patching.
#
# Prerequisites: git, and PlatformIO CLI on PATH (`pio`). Install pio with:
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
# Commit this patch was cut against (a `dev` commit -- that is the branch MeshCore takes work
# on). Newer MeshCore may need the edits re-applied by hand (see README "Updating").
# Override with MESHCORE_COMMIT= to track upstream.
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
