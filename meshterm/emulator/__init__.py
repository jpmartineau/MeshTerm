# SPDX-License-Identifier: Apache-2.0
"""The emulator: MeshTerm drawing a handheld's display itself, on the device or in a window.

**Not a strict emulator.** Nothing here runs the handheld's processor or its operating
system: MeshTerm runs as itself, on whatever machine runs this. What is reproduced is the
device's *display and keyboard* — its panel's pixel size, the character grid it divides
into, the font every glyph is drawn in, the colours it can show and how bold changes them,
and the keys that drive the F-key lane — so the screen in the window obeys the same
constraints the device's would. It mimics the display; it doesn't emulate the machine.

It began as the Cardputer Zero's *console host* — the program a text app runs inside — and
on that device it is still exactly that.
The Cardputer Zero has no text console an app may use: its launcher owns the screen, the
kernel's framebuffer console is switched off, and a store app may not run as root to
switch it back. What an app *does* get is the framebuffer itself and the keyboard's event
device. So MeshTerm brings its own terminal: this package runs the ordinary TUI in-process,
with prompt_toolkit writing to a fixed-size output whose bytes go to a small VT parser
(:mod:`.vt`) rather than to any terminal, and reading from a pipe this package types keys
into (:mod:`.keys`). The parsed cell grid is drawn into pixels with a 6×12 bitmap font
(:mod:`.font`, :mod:`.raster`), and the pixels go to one of two front ends:

* on the Cardputer Zero, the framebuffer the launcher hands over, with keys from the
  keyboard's event device (:mod:`.framebuffer`);
* on a desktop, a window drawing the device's panel scaled up, with keys from the desktop
  keyboard (:mod:`.window`) — the device's own code path everywhere except the two ends.

Nothing in the TUI knows it is emulated. The session already takes its input and output
from prompt_toolkit's app session wherever its own terminal wrappers are off, which they
are on these platforms, so this package only has to set that session up and call the CLI
the way a shell would (:mod:`.run`).
"""
