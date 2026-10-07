#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# build-deb -- build the Debian package of MeshTerm for the Cardputer Zero's app store.
#
# Usage: scripts/cardputer-zero/build-deb.sh [REVISION]
#
# Run it on arm64 Debian 13 ("Trixie") with Python 3.13: on the Cardputer Zero itself, or
# in a debian:trixie container on an arm64 machine. The package holds compiled code
# (spidev, pycryptodome, and others), so it must be built on the system that it is for.
# Build it from a clean checkout of a release tag. pip builds MeshTerm from this tree, and
# it also takes the files in meshterm/ that git does not track. On the Cardputer Zero, /tmp
# is in memory and too small for the build: set TMPDIR to a directory on the card.
#
# The package depends only on python3. All else is inside it:
#
#   /usr/lib/meshterm/site      MeshTerm and its Python packages, compiled to bytecode
#   /usr/lib/meshterm/fonts     Terminus 6x12, regular and bold, and its licence (the OFL)
#   /usr/share/APPLaunch/       the launcher entry, its icon, and the wrapper that it runs
#   /usr/bin/meshterm           the command line, for a user on SSH
#   /usr/share/doc/meshterm/    LICENSE, NOTICE, THIRD-PARTY-NOTICES.txt, and copyright
#
# We add no other Debian dependency on purpose. When a dependency is not installed, the
# store app runs `apt-get install`, but it never runs `apt-get update`. On a device with
# old package lists, that install can fail. For the same reason the package carries the
# font: Terminus is not in the repository or in the wheel, but a launcher app has no
# terminal in which the user can run `meshterm emulate --fetch-fonts`.
#
# REVISION is the Debian revision, 1 by default. Increase it to publish a new package of
# the same MeshTerm version, because the store refuses a version that is not newer.
# Set TERMINUS_ARCHIVE to the path of terminus-font-4.49.1.tar.gz if the download fails.
# The build checks that archive against the same digest.
set -eu
umask 022

HERE=$(cd "$(dirname "$0")" && pwd)
SOURCE=$(cd "$HERE/../.." && pwd)
REVISION=${1:-1}
PYTHON=/usr/bin/python3

die() {
    echo "build-deb: $*" >&2
    exit 1
}

command -v dpkg-deb >/dev/null || die "no dpkg-deb here -- run this on Debian"
[ "$(dpkg --print-architecture)" = arm64 ] || die "this is not arm64 -- the package holds arm64 code"
[ "$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" = 3.13 ] ||
    die "$PYTHON is not Python 3.13 -- the package is for Debian 13"
case "$REVISION" in
*[!0-9]* | "") die "the revision must be a number, not '$REVISION'" ;;
esac

VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$SOURCE/meshterm/__init__.py")
[ -n "$VERSION" ] || die "no __version__ in $SOURCE/meshterm/__init__.py"
DEB_VERSION="$VERSION-$REVISION"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/root"
LIB="$STAGE/usr/lib/meshterm"
DOC="$STAGE/usr/share/doc/meshterm"
APPLAUNCH="$STAGE/usr/share/APPLaunch"

echo "building meshterm $DEB_VERSION from $SOURCE"

# A venv for the tools of the build only. It has a current pip and the `packaging` library
# that packaging/notices.py imports. Its base is the system Python, so the compiled
# packages that its pip builds are correct for the Python that runs them.
"$PYTHON" -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --upgrade pip packaging

# MeshTerm and its packages, with the radio of the Cap LoRa-1262 (the `spi` extra).
# --no-compile, because the bytecode is compiled below in a different mode.
"$WORK/venv/bin/pip" install --quiet --no-compile --target "$LIB/site" "$SOURCE[spi]"
# pip writes console scripts whose first line names the build venv. The wrappers below
# replace them.
rm -rf "$LIB/site/bin"

# The licences: MeshTerm's own, and the licence of each package in the site directory.
mkdir -p "$DOC"
cp "$SOURCE/LICENSE" "$SOURCE/NOTICE" "$DOC/"
"$WORK/venv/bin/python" "$SOURCE/packaging/notices.py" "$DOC/THIRD-PARTY-NOTICES.txt" "$LIB/site"
cat >"$DOC/copyright" <<EOF
MeshTerm $VERSION
Source: https://github.com/jpmartineau/MeshTerm

MeshTerm is licensed under the Apache License, Version 2.0. Refer to LICENSE and NOTICE in
this directory. The name "MeshTerm" and its logo are trademarks. NOTICE has the terms.

The Python packages in /usr/lib/meshterm/site have their own licences. Refer to
THIRD-PARTY-NOTICES.txt in this directory.

The Terminus font in /usr/lib/meshterm/fonts is licensed under the SIL Open Font
License, Version 1.1. Refer to /usr/lib/meshterm/fonts/OFL.TXT.
EOF

# Terminus, downloaded and checked against its digest by meshterm.emulator.fetch.
MESHTERM_EMULATOR_FONTS="$LIB/fonts" PYTHONPATH="$LIB/site" "$PYTHON" -s -c '
import os, sys
from pathlib import Path
from meshterm.emulator.fetch import install_terminus
archive = os.environ.get("TERMINUS_ARCHIVE")
install_terminus(Path(archive) if archive else None)
'

# Bytecode. The user cannot write in /usr/lib, so Python cannot cache bytecode there at
# run time. Without this step, each start compiles all of MeshTerm again, and the start is
# several seconds slower. "unchecked-hash": the files never change after the install, so
# Python does not compare them with the source at each import.
"$PYTHON" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$LIB/site" >/dev/null

# The wrappers. PYTHONPATH is exported, so that a child process of MeshTerm finds the same
# packages. -s keeps the packages of the user's own site directory out.
wrapper() {
    cat >"$1" <<EOF
#!/bin/sh
# $2
export PYTHONPATH=/usr/lib/meshterm/site
export MESHTERM_EMULATOR_FONTS="\${MESHTERM_EMULATOR_FONTS:-/usr/lib/meshterm/fonts}"
exec /usr/bin/python3 -s -m $3 "\$@"
EOF
    chmod 755 "$1"
}
mkdir -p "$STAGE/usr/bin" "$APPLAUNCH/bin" "$APPLAUNCH/applications" "$APPLAUNCH/share/images"
wrapper "$STAGE/usr/bin/meshterm" "MeshTerm's command line." meshterm
wrapper "$APPLAUNCH/bin/meshterm" "MeshTerm from the launcher: it draws on the display itself." meshterm.emulator

# The launcher entry. Terminal=false gives the app the display and the keyboard.
cat >"$APPLAUNCH/applications/meshterm.desktop" <<EOF
[Desktop Entry]
Name=MeshTerm
Exec=/usr/share/APPLaunch/bin/meshterm
Icon=share/images/meshterm.png
Terminal=false
Type=Application
EOF
install -m 644 "$HERE/meshterm.png" "$APPLAUNCH/share/images/meshterm.png"

# Make sure that the bundle runs before it is packaged: the help, a tool's help (the tools
# are found at run time), the written pages (the specimen), and the font of the display.
# MESHTERM_HOME keeps the files that a run writes out of the home directory of the builder.
check() {
    MESHTERM_HOME="$WORK/home" MESHTERM_EMULATOR_FONTS="$LIB/fonts" PYTHONPATH="$LIB/site" \
        "$PYTHON" -s "$@" >/dev/null
}
check -m meshterm --help
check -m meshterm contacts --help
check -m meshterm specimen
check -c 'from meshterm.emulator.font import find_font; find_font()'

mkdir -p "$STAGE/DEBIAN"
cat >"$STAGE/DEBIAN/control" <<EOF
Package: meshterm
Version: $DEB_VERSION
Architecture: arm64
Maintainer: Jean-Pierre Martineau <johnputer@meshterm.net>
Installed-Size: $(du -sk "$STAGE" | cut -f1)
Depends: python3 (>= 3.13), python3 (<< 3.14)
Section: hamradio
Priority: optional
Homepage: https://meshterm.net
Description: full-featured MeshCore client for the Cardputer Zero
 MeshTerm is a full-featured TUI MeshCore client. On the Cardputer Zero it
 draws on the display itself, and it runs the Cap LoRa-1262 as its own radio.
 It can also connect to a companion over USB, Bluetooth, or TCP.
EOF

mkdir -p "$SOURCE/dist"
OUT="$SOURCE/dist/meshterm_${DEB_VERSION}_arm64.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$OUT" >/dev/null
echo "built $OUT ($(du -h "$OUT" | cut -f1))"
