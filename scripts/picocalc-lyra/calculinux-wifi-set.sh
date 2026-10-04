#!/bin/sh
# wifi-set -- interactively join a Wi-Fi network on wlan0 (iwd-managed).
#
# Usage: wifi-set
#
# Shows known and nearby networks for reference, asks for an SSID and a
# password, writes the credential the way iwd expects it
# (/var/lib/iwd/<SSID>.psk -- the same pattern calculinux-setup.sh uses for
# first-boot provisioning), then asks iwd to join it right away. Re-execs
# itself under sudo if not already root, since /var/lib/iwd is root-only.
#
# Unrelated to /etc/wifi-kick.sh: that one only nudges the rtl8xxxu dongle
# past a cold-boot firmware-load race so iwd can associate to whatever
# network is already known -- it does not care which network that is.

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
