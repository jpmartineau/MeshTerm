#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# launcher-entry -- put MeshTerm on the Cardputer Zero's app launcher, from a checkout.
#
# Usage: scripts/cardputer-zero/launcher-entry.sh [install|remove]
#
# The stock launcher (APPLaunch) lists what it finds in
# /usr/share/APPLaunch/applications. An entry with Terminal=false gets the panel and the
# keyboard to itself, the launcher naming them in APPLAUNCH_LINUX_FBDEV_DEVICE and
# APPLAUNCH_LINUX_KEYBOARD_DEVICE -- which is how `python -m meshterm.emulator` knows to
# draw on the panel rather than open a window. Holding Esc for three seconds still ends it:
# the launcher sends SIGTERM, and MeshTerm takes that as a quit.
#
# This is the developer's entry, run from a source checkout with its venv at .venv (see
# docs/devices/cardputer-zero.md). It writes into /usr/share, so it asks sudo for the two
# files and nothing else, then restarts the launcher so the icon appears. A store package
# will replace it; `remove` takes it out again.
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
Icon=share/images/meshterm.png
Terminal=false
Type=Application
EOF
    sudo install -m 644 "$entry" "$APPS/meshterm.desktop"
    sudo install -m 644 "$HERE/meshterm.png" "$IMAGES/meshterm.png"
    rm -f "$entry"
    echo "installed $APPS/meshterm.desktop (runs $PYTHON)"
    ;;
remove)
    sudo rm -f "$APPS/meshterm.desktop" "$IMAGES/meshterm.png"
    echo "removed MeshTerm from the launcher"
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
