#!/bin/sh
# wifi-set -- interactively join a Wi-Fi network on wlan0 (iwd-managed).
#
# Usage: wifi-set
#
# The script shows the known and nearby networks for reference. It asks for an SSID and a
# password. It writes the credential in the form that iwd expects
# (/var/lib/iwd/<SSID>.psk, the same pattern that calculinux-setup.sh uses for
# provisioning at the first boot). Then it asks iwd to join the network at once. If the
# user is not root, the script runs itself again under sudo, because /var/lib/iwd is
# only for root.
#
# This script is not related to /etc/wifi-kick.sh. That script only nudges the rtl8xxxu
# dongle past a race in the firmware load at a cold boot, so that iwd can associate to a
# network that is already known. It does not care which network that is.

set -eu

if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

IFACE=wlan0

trap 'stty echo 2>/dev/null || true' EXIT INT TERM

echo "Known networks:"
iwctl known-networks list
echo

echo "Scanning for nearby networks..."
iwctl station "$IFACE" scan >/dev/null 2>&1 || true
sleep 2
iwctl station "$IFACE" get-networks || true
echo

printf "SSID to join: "
read -r ssid
[ -n "$ssid" ] || { echo "No SSID given, stopping." >&2; exit 1; }

printf "Password for \"%s\" (blank for an open network): " "$ssid"
stty -echo 2>/dev/null || true
read -r pass
stty echo 2>/dev/null || true
echo

if [ -n "$pass" ]; then
    mkdir -p /var/lib/iwd
    printf '[Security]\nPassphrase=%s\n' "$pass" > "/var/lib/iwd/$ssid.psk"
    chmod 600 "/var/lib/iwd/$ssid.psk"
fi

echo "Joining \"$ssid\"..."
iwctl station "$IFACE" connect "$ssid"

sleep 2
iwctl station "$IFACE" show
