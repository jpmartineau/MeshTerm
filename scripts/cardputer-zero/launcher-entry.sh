#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# launcher-entry -- put MeshTerm on the app launcher of the Cardputer Zero, from a checkout.
#
# Usage: scripts/cardputer-zero/launcher-entry.sh [install|remove]
#
# The stock launcher (APPLaunch) lists the entries that it finds in
# /usr/share/APPLaunch/applications. An entry with Terminal=false gets the display and the
# keyboard for itself. The launcher names them in APPLAUNCH_LINUX_FBDEV_DEVICE and
# APPLAUNCH_LINUX_KEYBOARD_DEVICE. This is how `python -m meshterm.emulator` knows that it
# must draw on the display and not open a window. If the user holds Esc for three seconds,
# the app still ends. The launcher sends SIGTERM, and MeshTerm treats it as a quit.
#
# This is the entry for developers. Run it from a source checkout that has its venv at
# .venv (refer to docs/devices/cardputer-zero.md). It writes into /usr/share, so it asks
# sudo for the two files and for nothing else. Then it restarts the launcher, so that the
# icon appears. `remove` takes it out again.
#
# The entry has its own file names and a negative icon (meshterm-dev.png). Thus it is not
# the same as the entry of the package from the store (build-deb.sh), and the two can be
# installed together. The package owns meshterm.desktop and meshterm.png, and this script
# never touches them.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
CHECKOUT=$(cd "$HERE/../.." && pwd)
PYTHON="$CHECKOUT/.venv/bin/python"
APPS=/usr/share/APPLaunch/applications
IMAGES=/usr/share/APPLaunch/share/images

case "${1:-install}" in
install)
    if [ ! -d "$APPS" ]; then
        echo "no $APPS here -- this is for the Cardputer Zero's stock launcher" >&2
        exit 1
    fi
    if ! "$PYTHON" -c "import meshterm.emulator" 2>/dev/null; then
        echo "$PYTHON can't import MeshTerm -- make the venv first:" >&2
        echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[spi]'" >&2
        exit 1
    fi
    entry=$(mktemp)
    cat >"$entry" <<EOF
[Desktop Entry]
Name=MeshTerm
Exec=$PYTHON -m meshterm.emulator
Icon=share/images/meshterm-dev.png
Terminal=false
Type=Application
EOF
    sudo install -m 644 "$entry" "$APPS/meshterm-dev.desktop"
    sudo install -m 644 "$HERE/meshterm-dev.png" "$IMAGES/meshterm-dev.png"
    rm -f "$entry"
    echo "installed $APPS/meshterm-dev.desktop (runs $PYTHON)"
    ;;
remove)
    sudo rm -f "$APPS/meshterm-dev.desktop" "$IMAGES/meshterm-dev.png"
    echo "removed the checkout's MeshTerm from the launcher"
    ;;
*)
    echo "usage: $0 [install|remove]" >&2
    exit 2
    ;;
esac

if [ "${1:-install}" = install ] && [ ! -f "$HOME/.meshterm/fonts/ter-u12n.bdf" ]; then
    echo "fetch the screen's font once: $CHECKOUT/.venv/bin/meshterm emulate --fetch-fonts"
fi
systemctl --user restart APPLaunch.service 2>/dev/null || echo "restart the launcher to see the change"
