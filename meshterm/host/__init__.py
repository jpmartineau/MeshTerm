# SPDX-License-Identifier: Apache-2.0
"""The console host: MeshTerm drawing its own pixels, for a panel no terminal runs on.

The Cardputer Zero has no text console an app may use. Its launcher owns the screen, the
kernel's framebuffer console is switched off, and a store app may not run as root to
switch it back. What an app *does* get is the framebuffer itself and the keyboard's event
device. So MeshTerm brings its own terminal: this package runs the ordinary TUI in-process,
with prompt_toolkit writing to a fixed 53×14 output whose bytes go to a small VT parser
(:mod:`.vt`) rather than to any terminal, and reading from a pipe the host writes keys into
(:mod:`.keys`). The parsed cell grid is drawn into pixels with a 6×12 bitmap font
(:mod:`.font`, :mod:`.raster`), and the pixels go wherever the host is showing them:

* on the device, the framebuffer the launcher hands over, with keys from the keyboard's
  event device (:mod:`.device`);
* on a desktop, a window drawing the same 320×170 panel scaled up, with keys from the
  desktop keyboard (:mod:`.sim`) — the simulator, which is the device's own code path
  everywhere except the two ends.

Nothing in the TUI knows it is hosted. The session already takes its input and output
from prompt_toolkit's app session wherever its own terminal wrappers are off, which they
are on this platform, so the host only has to set that session up and call the CLI the
way a shell would (:mod:`.run`).
"""
