#!/bin/sh
# Hand this to drive_console.py as --exe to run MeshTerm under the freeze watchdog.
# It runs MeshTerm through watchdog.py, which appends events to $WATCHDOG_LOG
# (default ~/tmp/watchdog.log): event-loop pings, stack samples of every thread
# while the loop is stuck, RSS/swap/major faults, and map tile and raster timing.
#
# The checkout is $MESHTERM_REPO, or ~/MeshTerm -- never a hardcoded /home/<someone>,
# since this runs as whatever user the device deployed under.
REPO="${MESHTERM_REPO:-$HOME/MeshTerm}"
cd "$REPO" || exit 1
exec "$REPO/.venv/bin/python" "$REPO/watchdog.py" "$@"
