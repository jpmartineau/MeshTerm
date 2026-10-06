# SPDX-License-Identifier: Apache-2.0
"""The emulator: MeshTerm draws a handheld's display itself, on the handheld or in a window.

**Not a strict emulator.** Nothing here runs the processor or the operating system of the
handheld. MeshTerm runs as itself, on the machine that runs this package. The emulator
copies the display and the keyboard of the handheld: the size of the display in pixels,
the character grid on the display, the font that draws each glyph, the colours that the
display can show and how bold changes them, and the keyboard keys that drive the F-key
lane. Thus the screen in the window obeys the same constraints as the screen of the
handheld. The emulator copies the display. It does not emulate the machine.

The emulator started as the *console host* of the Cardputer Zero: the program that a text
app runs in. On that handheld, it is still exactly that. The Cardputer Zero has no text
console that an app can use. Its launcher owns the screen, the framebuffer console of the
kernel is off, and a store app cannot run as root to turn it on again. An app gets only
the framebuffer itself and the event device file of the keyboard. Thus MeshTerm brings its
own terminal. This package runs the ordinary TUI in the same process:

* prompt_toolkit writes to an output of a fixed size. The bytes of that output go to a
  small VT parser (:mod:`.vt`) instead of to a terminal.
* prompt_toolkit reads from a pipe, and this package types key presses into that pipe
  (:mod:`.keys`).

A 6×12 bitmap font draws the parsed cell grid into pixels (:mod:`.font`, :mod:`.raster`).
The pixels go to one of two front ends:

* On the Cardputer Zero: the framebuffer that the launcher gives, with key presses from
  the event device file of the keyboard (:mod:`.framebuffer`).
* On a desktop: a window that draws the display of the handheld at a larger scale, with
  key presses from the desktop keyboard (:mod:`.window`). The code path is the same as on
  the handheld, except at these two ends.

Nothing in the TUI knows that it is emulated. When the terminal wrappers of the session
are off, the session takes its input and output from the app session of prompt_toolkit.
The wrappers are off on these platforms. Thus this package must only set up that session
and call the CLI as a shell does (:mod:`.run`).
"""
