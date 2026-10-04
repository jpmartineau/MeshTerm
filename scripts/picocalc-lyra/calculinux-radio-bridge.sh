#!/bin/sh
# calculinux-radio-bridge.sh -- put the UART-attached MeshCore radio on TCP for MeshTerm
# (Luckfox Lyra / PicoCalc running Calculinux).
#
# The hardware this serves: a Seeed XIAO nRF52840 + Wio-SX1262 running a custom MeshCore
# companion firmware whose frame protocol is bound to the XIAO's hardware UART1 (D6=TX,
# D7=RX) at 115200 8N1, soldered to the Lyra's UART1. UART0 carries the Linux console, so
# the radio lives on /dev/ttyS1 -- which this script never touches beyond opening it.
#
# Why a bridge instead of pointing MeshTerm straight at the port: pyserial's Linux
# enumeration hides platform-bus UARTs ("hide non-present internal serial ports"), so a
# soldered SoC port like /dev/ttyS1 never appears in discovery or the liveness poll. The
# TCP transport has none of those problems -- MeshTerm already lists a TCP profile on the
# startup splash and tracks the socket for liveness -- and the same bridge makes the radio
# reachable from the LAN (`meshterm --tcp <lyra-ip>:5000` on another machine).
#
# Run as root on the device:
#
#     sh calculinux-radio-bridge.sh
#
# It is idempotent -- safe to re-run; each phase checks before it acts.
#
#   1. UART sanity    /dev/ttyS1 exists, the console isn't on it, no getty owns it.
#   2. bridge         A stdlib-only python UART<->TCP pump (no pyserial, no venv) installed
#                     as /etc/meshterm-radio-bridge.py + a systemd service, enabled at boot.
#                     It serves ONE client at a time -- two clients would interleave
#                     companion frames -- which is why this isn't a `socat ... fork` line.
#   3. profile        A `[profiles.radio]` TCP profile (127.0.0.1:5000) in the deploy
#                     user's ~/.meshterm/config.toml, made the default profile, so a bare
#                     `meshterm` lists the radio on the splash and `-p radio` connects.
#   4. report         Service state and how to connect, locally and over the LAN.
#
# What this script does NOT do: it never routes UART1 to physical pads. On stock
# Calculinux the DT enables the UART1 controller (hence /dev/ttyS1 existing) without
# applying its pinctrl, so TX/RX reach no pad and this pump would faithfully serve a port
# that is electrically dead. Pin routing now lives in xiao-radio/uart1-mux.py, which uses
# a different pad pair (header GP4/GP5) than this script's header once assumed -- run that
# (via xiao-radio/lyra-setup.sh) first if you need the radio actually wired up.
set -eu

# --- knobs (override via the environment) ----------------------------------------------
DEPLOY_USER="${DEPLOY_USER:-meshterm}"
RADIO_PORT="${RADIO_PORT:-/dev/ttyS1}"     # the Lyra UART the XIAO is soldered to
RADIO_BAUD="${RADIO_BAUD:-115200}"         # must match the firmware's Serial1.begin()
BIND_ADDR="${BIND_ADDR:-0.0.0.0}"          # 127.0.0.1 to keep the radio off the LAN
TCP_PORT="${TCP_PORT:-5000}"               # the conventional MeshCore-over-TCP port
PROFILE_NAME="${PROFILE_NAME:-radio}"

BRIDGE_PY="/etc/meshterm-radio-bridge.py"
UNIT="meshterm-radio-bridge.service"
CONFIG="/home/$DEPLOY_USER/.meshterm/config.toml"

log()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (writes /etc, installs a systemd service)"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found (Calculinux should have systemd)"
PY3="$(command -v python3 || true)"
[ -n "$PY3" ] || die "python3 not found -- run calculinux-setup.sh first (opkg python packages)"

TTY_NAME="$(basename "$RADIO_PORT")"

# --- 1. UART sanity --------------------------------------------------------------------
log "1/4  UART sanity ($RADIO_PORT)"

[ -c "$RADIO_PORT" ] || die "$RADIO_PORT is not a character device
   the UART1 overlay may be disabled -- enable it (luckfox-config / the board's dtbo
   mechanism), reboot, and re-run this script"

# The kernel console must stay on UART0; a console sharing the radio's UART would corrupt
# the companion frame stream in both directions.
if grep -q "console=$TTY_NAME" /proc/cmdline; then
    die "the kernel console is on $TTY_NAME -- refusing to bridge over it"
fi
info "console is not on $TTY_NAME"

# Same story for a login getty: it would eat radio bytes and echo garbage back.
if ps w | grep -v grep | grep getty | grep -q "$TTY_NAME"; then
    die "a getty holds $TTY_NAME -- free it first:
     systemctl disable --now serial-getty@$TTY_NAME.service"
fi
info "no getty on $TTY_NAME"

# --- 2. bridge script + service --------------------------------------------------------
log "2/4  UART<->TCP bridge"

cat > "$BRIDGE_PY" <<'PYEOF'
#!/usr/bin/env python3
"""MeshCore companion UART <-> TCP bridge.

A byte-transparent pump between a companion radio on a hardware UART and a single TCP
client speaking the MeshCore companion frame protocol. Deliberately stdlib-only (termios,
not pyserial) so it runs on the bare system python and survives any venv rebuild.

One client at a time, by design: the companion protocol is a stateful request/response
stream, and two clients on one radio would interleave frames into garbage. The listener
keeps a backlog of 1, so the next client simply waits until the current one leaves.

Usage: meshterm-radio-bridge.py <serial-dev> <baud> <bind-addr> <tcp-port>
"""

import os
import socket
import sys
import termios
import threading
import tty


def open_serial(path, baud):
    """Open the UART raw at 8N1/<baud> with no flow control, and flush stale bytes."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    # cfmakeraw: 8-bit clean, no echo/signals/CR-LF translation, VMIN=1/VTIME=0 so reads
    # block for at least one byte then return whatever arrived.
    tty.setraw(fd)
    try:
        speed = getattr(termios, "B%d" % baud)
    except AttributeError:
        sys.exit("unsupported baud rate: %d" % baud)
    attrs = termios.tcgetattr(fd)
    attrs[2] &= ~(termios.CSTOPB | termios.CRTSCTS)  # one stop bit, no RTS/CTS
    attrs[2] |= termios.CLOCAL | termios.CREAD       # no modem lines on a soldered link
    attrs[4] = speed
    attrs[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)  # drop anything queued from before we opened
    return fd


def main():
    if len(sys.argv) != 5:
        sys.exit("usage: meshterm-radio-bridge.py <serial-dev> <baud> <bind-addr> <tcp-port>")
    dev, baud, bind, port = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    fd = open_serial(dev, baud)
    state = {"client": None}
    lock = threading.Lock()

    def serial_pump():
        # Serial -> client, forever. Bytes heard while no client is attached are
        # discarded, so the kernel buffer never backs up and a new client starts on
        # live frames rather than a stale backlog.
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError:
                os._exit(1)  # the UART died; let systemd restart us
            if not data:
                os._exit(1)
            with lock:
                client = state["client"]
            if client is None:
                continue
            try:
                client.sendall(data)
            except OSError:
                pass  # client vanished mid-send; the accept loop sees it via recv

    threading.Thread(target=serial_pump, daemon=True).start()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((bind, port))
    listener.listen(1)  # one client owns the radio; the next waits its turn
    while True:
        client, _peer = listener.accept()
        # Companion frames are small and latency-sensitive; don't let Nagle batch them.
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with lock:
            state["client"] = client
        try:
            while True:
                data = client.recv(4096)
                if not data:
                    break
                while data:  # os.write on a UART may accept only part of a burst
                    n = os.write(fd, data)
                    data = data[n:]
        except OSError:
            pass
        finally:
            with lock:
                state["client"] = None
            client.close()


if __name__ == "__main__":
    main()
PYEOF
chmod +x "$BRIDGE_PY"
info "installed $BRIDGE_PY"

cat > "/etc/systemd/system/$UNIT" <<UNITEOF
[Unit]
Description=MeshCore companion UART-TCP bridge ($RADIO_PORT @ $RADIO_BAUD)
# Pointless without the UART; the node appears early in boot when the overlay is on.
ConditionPathExists=$RADIO_PORT
After=network.target

[Service]
ExecStart=$PY3 $BRIDGE_PY $RADIO_PORT $RADIO_BAUD $BIND_ADDR $TCP_PORT
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1 || info "could not enable $UNIT"
systemctl restart "$UNIT" || true
info "installed + enabled $UNIT"

# --- 3. MeshTerm profile ---------------------------------------------------------------
log "3/4  MeshTerm profile [$PROFILE_NAME]"

mkdir -p "$(dirname "$CONFIG")"
[ -f "$CONFIG" ] || : > "$CONFIG"

if grep -q "^\[profiles\.$PROFILE_NAME\]" "$CONFIG"; then
    info "profile already present -- leaving config.toml untouched"
else
    # default_profile is a top-level key, so it must sit ABOVE any [table] header --
    # prepend it rather than appending after the profile blocks.
    if ! grep -q '^default_profile' "$CONFIG"; then
        TMP="$CONFIG.tmp"
        printf 'default_profile = "%s"\n\n' "$PROFILE_NAME" > "$TMP"
        cat "$CONFIG" >> "$TMP"
        mv "$TMP" "$CONFIG"
        info "made [$PROFILE_NAME] the default profile"
    fi
    cat >> "$CONFIG" <<PROFEOF

[profiles.$PROFILE_NAME]
transport = "tcp"
host = "127.0.0.1"
tcp_port = $TCP_PORT
description = "XIAO nRF52840 + Wio-SX1262 via the UART1 bridge"
PROFEOF
    info "wrote [profiles.$PROFILE_NAME] -> 127.0.0.1:$TCP_PORT"
fi
chown "$DEPLOY_USER:$DEPLOY_USER" "$(dirname "$CONFIG")" "$CONFIG" 2>/dev/null || true

# --- 4. report -------------------------------------------------------------------------
log "4/4  done"
if systemctl is-active --quiet "$UNIT"; then
    info "bridge is running on $BIND_ADDR:$TCP_PORT (logs: journalctl -u $UNIT)"
else
    info "bridge is NOT running -- inspect it with: journalctl -u $UNIT"
fi
info "on this device:   log in as $DEPLOY_USER and run 'meshterm' (the $PROFILE_NAME"
info "                  profile is listed on the splash), or 'meshterm -p $PROFILE_NAME'"
if [ "$BIND_ADDR" = "0.0.0.0" ]; then
    info "over the LAN:     meshterm --tcp <this-device-ip>:$TCP_PORT"
    info "                  (one client at a time; anyone on the LAN can drive the radio --"
    info "                  re-run with BIND_ADDR=127.0.0.1 to keep it local)"
fi
