#!/bin/sh
# lyra-setup.sh -- make the UART-attached XIAO radio work on the Luckfox Lyra (Calculinux).
#
# Runs ON THE DEVICE as root. Idempotent -- safe to re-run.
#
#   1. installs uart1-mux.py and a systemd oneshot that re-applies the UART1->GP4/GP5 pin
#      routing on every boot (Calculinux leaves /dev/ttyS1 wired to no pad otherwise)
#   2. adds the MeshTerm user to the `dialout` group so it can open /dev/ttyS1
#   3. writes a default MeshTerm serial profile pointing at /dev/ttyS1
#
# The MeshTerm user defaults to `meshterm`; override with:  MT_USER=youruser sh lyra-setup.sh
#
# Prereq: the XIAO must be flashed with the radio firmware (see build-firmware.sh / flash.py)
# and soldered per the README wiring table.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MT_USER="${MT_USER:-meshterm}"
MT_HOME=$(getent passwd "$MT_USER" 2>/dev/null | cut -d: -f6)
[ -n "$MT_HOME" ] || MT_HOME="/home/$MT_USER"
MUX_DST=/usr/local/bin/uart1-radio-mux.py
UNIT=/etc/systemd/system/uart1-radio-mux.service

[ "$(id -u)" = 0 ] || { echo "ERROR: run as root"; exit 1; }
[ -e /dev/ttyS1 ] || { echo "ERROR: /dev/ttyS1 missing -- is UART1 enabled in the DT?"; exit 1; }

echo ">> installing pin-mux helper -> $MUX_DST"
mkdir -p "$(dirname "$MUX_DST")"
cp "$HERE/uart1-mux.py" "$MUX_DST"
chmod 755 "$MUX_DST"

echo ">> installing + enabling systemd service"
cat > "$UNIT" <<EOF
[Unit]
Description=Route RK3506 UART1 to GP4/GP5 for the MeshCore XIAO radio
# Ordered on basic.target, not multi-user.target: WantedBy=multi-user.target below already
# pulls this in as part of reaching multi-user.target, so After=multi-user.target would be
# an ordering cycle. The mux should still run early enough to precede login/MeshTerm start.
After=basic.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $MUX_DST
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable uart1-radio-mux.service >/dev/null 2>&1 || true
systemctl restart uart1-radio-mux.service
echo "   $(systemctl is-enabled uart1-radio-mux.service) / $(systemctl is-active uart1-radio-mux.service)"

echo ">> granting $MT_USER access to /dev/ttyS1 (dialout group)"
if id "$MT_USER" >/dev/null 2>&1; then
    if groups "$MT_USER" 2>/dev/null | grep -qw dialout; then
        echo "   already in dialout"
    else
        usermod -aG dialout "$MT_USER" && echo "   added (takes effect on next login of $MT_USER)"
    fi
else
    echo "   WARN: user $MT_USER not found -- skipping dialout"
fi

echo ">> default MeshTerm profile -> $MT_HOME/.meshterm/config.toml"
CFG="$MT_HOME/.meshterm/config.toml"
if [ -e "$CFG" ]; then
    echo "   config already exists -- leaving it untouched."
    echo "   To use the radio, ensure it contains a serial profile:"
    echo '     default_profile = "picocalc"'
    echo '     [profiles.picocalc]'
    echo '       port = "/dev/ttyS1"'
else
    mkdir -p "$(dirname "$CFG")"
    cat > "$CFG" <<'EOF'
# MeshTerm configuration
# XIAO nRF52840 + Wio-SX1262 radio, reached over the Lyra hardware UART1 (/dev/ttyS1).
default_profile = "picocalc"

[profiles.picocalc]
port = "/dev/ttyS1"
baudrate = 115200
transport = "serial"
default_tx_power = 22
description = "XIAO nRF52840 + SX1262 via Lyra UART1 on GP4/GP5"
EOF
    chown -R "$MT_USER:$MT_USER" "$(dirname "$CFG")" 2>/dev/null || true
    echo "   written."
fi

echo ""
echo "DONE.  Verify the radio answers:"
echo "    su - $MT_USER -c 'meshterm'"
echo "(the mux is now active and will re-apply on every boot)"
