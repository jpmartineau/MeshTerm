# SPDX-License-Identifier: Apache-2.0
"""Make the screenshots of the website from the demo mesh, the same at each run.

    python scripts/screenshots.py [--out DIR] [--only NAME ...]

The script runs MeshTerm three times, one time for each set: ``terminal`` (the desktop, in
an 80×24 terminal), ``cardputer-zero``, and ``picocalc-lyra``. Each run uses ``--demo``
(refer to :mod:`meshterm.demo`) and types the same keys: it opens the chat on Public,
the paths of a message, and the map, and it saves each screen as a PNG. The result is
``DIR/<set>/<screen>.png`` (``DIR`` is ``docs/screenshots`` by default).

The script also draws each wordmark of the startup splash (``meshterm/assets/splash``) as
``DIR/splash/logo.<width>.png``, one image pixel for each pixel of the font: the website
shows them as its splash, the wide mark on a wide screen and the narrow mark on a phone.

The screens are the same at each run, for four reasons:

* The demo writes the same history at each start.
* The clock of each run is set to 11:42 on Thursday 15 October 2026, Montréal time, the
  "now" of the demo script (``time-machine``, with ``TZ`` for the time zone). The clock
  then runs normally.
* The demo device plays no background traffic during a capture
  (:data:`meshterm.demo.device.PLAY_BACKGROUND`). Only the lines of the script arrive.
* The script waits until the screen has not changed for a moment before it saves it.

MeshTerm runs in this process, in the session of the emulator (:mod:`meshterm.emulator`):
its output goes to a VT parser, and the script types into a pipe. Thus no window opens.
A handheld set is drawn by the emulator's own raster, in its own font, at three times the
size of the display. The terminal set is drawn here, in a desktop font (Cascadia Mono by
default). This script draws the box, block, braille, and Powerline glyphs itself, as a
modern terminal does, so that the lines join and the map has no gaps.

The map needs its tiles. The first run downloads them into the tile cache of the demo
directory. The later runs read them from there.

What it needs, beside MeshTerm: ``pillow``, ``time-machine``, and ``fonttools``
(``pip install -e .[dev]`` installs them), and the Terminus font of the emulator
(``meshterm emulate --fetch-fonts``).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The sets, in the order on the website. The terminal set is the default there.
SETS = ("terminal", "cardputer-zero", "picocalc-lyra")

#: The name of the wordmarks in ``--only``. They need no run of MeshTerm.
SPLASH = "splash"

#: The 16 colours of the wordmarks: the Campbell scheme of Windows Terminal, which the
#: art was drawn in and the website uses.
CAMPBELL = (
    (12, 12, 12), (197, 15, 31), (19, 161, 14), (193, 156, 0),
    (0, 55, 218), (136, 23, 152), (58, 150, 221), (204, 204, 204),
    (118, 118, 118), (231, 72, 86), (22, 198, 12), (249, 241, 165),
    (59, 120, 255), (180, 0, 158), (97, 214, 214), (242, 242, 242),
)  # fmt: skip

#: The grid of the terminal set: a standard terminal.
TERMINAL_GRID = (80, 24)

#: The "now" of the demo script (``meshterm.demo.script.NOW_LOCAL``), on a fixed date:
#: Thursday 15 October 2026, 11:42 Eastern Daylight Time.
NOW = datetime(2026, 10, 15, 11, 42, tzinfo=timezone(timedelta(hours=-4)))

#: The time zone of the runs, in the form that the C runtime reads from ``TZ`` on each
#: system.
TZ = "EST5EDT"

#: The message whose paths the capture shows: the channel to type in the picker, how many
#: times to press ↑ from the newest message, and a part of its text near the start, which
#: the quote at the top of the dialog shows on each platform. The capture checks the quote,
#: and presses ↑ again until it finds the message, because a key press can be lost while
#: the transcript draws. In the dialog it then selects the second path: the first one is
#: often a straight line.
PATHS_MESSAGE = ("public", 2, "Hello from Beloeil")


def main(argv: list[str]) -> int:
    """Run one child process for each set, then report what each one wrote."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    parser.add_argument("--only", nargs="*", choices=(*SETS, SPLASH), default=[*SETS, SPLASH])
    parser.add_argument("--child", choices=SETS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return _child(args.child, args.out)
    status = 0
    if SPLASH in args.only:
        for path in _draw_splash(args.out / SPLASH):
            print(f"{SPLASH}: {path}", flush=True)
    for name in (name for name in args.only if name in SETS):
        print(f"{name}: capturing...", flush=True)
        done = subprocess.run(
            [sys.executable, __file__, "--child", name, "--out", str(args.out)],
            env=child_env(Path(tempfile.gettempdir()) / "meshterm-screenshots" / name),
            check=False,
        )
        if done.returncode:
            print(f"{name}: failed ({done.returncode})", flush=True)
            status = 1
    return status


def child_env(home: Path) -> dict[str, str]:
    """The environment of a child process that runs MeshTerm for a capture.

    Args:
        home: The MeshTerm directory of the run (``MESHTERM_HOME``). The demo writes its
            own directory under it again at each start, and keeps its tile cache there.
    """
    from meshterm.emulator.font import font_dir

    # The emulator finds its font under the config directory. Each run has a config
    # directory of its own, so it gets the font directory of this machine by name.
    fonts = os.environ.get("MESHTERM_EMULATOR_FONTS") or str(font_dir())
    return {
        **os.environ,
        "MESHTERM_HOME": str(home),
        "MESHTERM_EMULATOR_FONTS": fonts,
        "TZ": TZ,
        "MESHTERM_POWERLINE": "full",
        "MESHTERM_EMOJI": "1",
        # The two wraps of a real terminal (column pins, and the last column of a
        # Windows console) are for a terminal that measures glyphs in its own way.
        # Here the VT parser measures them as MeshTerm does.
        "MESHTERM_COLUMN_SNAP": "0",
        "MESHTERM_FULL_WIDTH": "0",
        "PYTHONIOENCODING": "utf-8",
    }


# --------------------------------------------------------------------------- the child


class Capture:
    """The front end of the emulator for a capture: it counts the frames and saves the grid."""

    def __init__(self, terminal, lock: threading.Lock) -> None:  # noqa: ANN001 - a Terminal
        """Keep the terminal and its lock."""
        self.terminal = terminal
        self.lock = lock
        self.frames = 0
        self.last = time.monotonic()

    def frame_ready(self) -> None:
        """Note the time of the newest frame."""
        self.frames += 1
        self.last = time.monotonic()

    def close(self) -> None:
        """Nothing to close: the capture has no window."""

    def settle(self, quiet: float = 1.2, timeout: float = 30.0) -> None:
        """Wait until no frame came for ``quiet`` seconds (or until ``timeout``)."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            time.sleep(0.05)
            if self.frames and time.monotonic() - self.last >= quiet:
                return

    def wait_for(self, text: str, timeout: float = 30.0) -> None:
        """Wait until ``text`` is on the screen.

        Raises:
            TimeoutError: The text did not come.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            with self.lock:
                if text in self.terminal.text():
                    return
            time.sleep(0.1)
        with self.lock:
            screen = self.terminal.text()
        raise TimeoutError(f"{text!r} did not appear. The screen:\n{screen}")

    def quote(self) -> str:
        """The quote of the message in the paths dialog: the line above its "heard" line."""
        with self.lock:
            lines = self.terminal.text().splitlines()
        for index, line in enumerate(lines):
            if index and "heard" in line and "time" in line:
                return lines[index - 1]
        return ""

    def grid(self) -> list[list[tuple[str, object]]]:
        """A copy of the cells of the screen."""
        with self.lock:
            return [list(row) for row in self.terminal.screen]


#: A tour: it gets the capture and a function that types text into MeshTerm.
Tour = Callable[[Capture, Callable[[str], None]], None]


def _child(name: str, out: Path) -> int:
    """Capture one set in this process. The parent set the environment."""
    target = out / name
    target.mkdir(parents=True, exist_ok=True)

    def tour(capture: Capture, type_text: Callable[[str], None]) -> None:
        def save(screen: str) -> None:
            path = target / f"{screen}.png"
            image = _draw(name, capture.grid(), capture.terminal.palette)
            image.save(path, optimize=True)
            print(f"{name}: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")

        _tour(capture, type_text, save)

    return run_demo(name, tour, background=False)


def run_demo(
    name: str, tour: Tour, *, background: bool, grid: tuple[int, int] = TERMINAL_GRID
) -> int:
    """Run MeshTerm with ``--demo`` in this process, as the set ``name``, and do ``tour``.

    The tour runs in a thread of its own while MeshTerm runs in the main thread. When the
    tour ends, it stops the whole process: exit status 0 if it completed, 1 if it raised
    an exception. The parent process set the environment (refer to :func:`child_env`).

    Args:
        name: One of :data:`SETS`.
        tour: What to do with MeshTerm.
        background: Whether the demo device plays the background traffic of the world
            (:data:`meshterm.demo.device.PLAY_BACKGROUND`).
        grid: The columns and the rows of the terminal set. A handheld set always has the
            grid of its display.

    Returns:
        1, which comes only if MeshTerm stopped before the tour ended it.
    """
    import time_machine

    traveller = time_machine.travel(NOW, tick=True)
    traveller.start()

    from prompt_toolkit.application.current import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output.color_depth import ColorDepth

    from meshterm import cli, platforms
    from meshterm.demo import device as demo_device
    from meshterm.emulator.devices import DEVICES
    from meshterm.emulator.run import _palette, panel_output
    from meshterm.emulator.vt import Terminal

    demo_device.PLAY_BACKGROUND = background
    handheld = DEVICES.get(name)
    if handheld is not None:
        cols, rows = handheld.platform.readable_cols, handheld.platform.readable_rows
        depth, platform = handheld.color_depth, handheld.id
    else:
        cols, rows = grid
        depth, platform = ColorDepth.TRUE_COLOR, "regular"
    lock = threading.Lock()
    with create_pipe_input() as pipe:
        terminal = Terminal(cols, rows, palette=_palette(), reply=pipe.send_text)
        capture = Capture(terminal, lock)

        def drive() -> None:
            try:
                tour(capture, pipe.send_text)
            except BaseException:  # noqa: BLE001 - report it, then stop the whole child
                traceback.print_exc()
                os._exit(1)
            os._exit(0)

        threading.Thread(target=drive, name="capture", daemon=True).start()
        output = panel_output(terminal, lock, capture.frame_ready, depth)
        with create_app_session(input=pipe, output=output), platforms.drawn_by_meshterm():
            try:
                cli.app(args=["--platform", platform, "--demo"], windows_expand_args=False)
            except SystemExit:
                pass
    return 1  # the tour ends the process. MeshTerm stopped before it.


def _tour(capture: Capture, type_text: Callable[[str], None], save: Callable[[str], None]) -> None:
    """Type the keys that reach each screen, and save each one."""
    from meshterm.emulator.keys import Key, encode

    def press(name: str = "", text: str = "", *, ctrl: bool = False, count: int = 1) -> None:
        for _ in range(count):
            type_text(encode(Key(name=name, text=text, ctrl=ctrl)))
            time.sleep(0.15)

    def typed(text: str) -> None:
        for char in text:
            type_text(char)
            time.sleep(0.05)

    # The main menu, after the direct message of the script arrived (6 s after the start).
    capture.wait_for("Chat", timeout=60)
    time.sleep(8.0)
    capture.settle()
    save("menu")
    # The chat, on Public.
    typed("chat")
    press("enter")
    capture.wait_for("Public")
    capture.settle()
    typed("public")
    press("enter")
    capture.wait_for("Gabriel")
    capture.settle()
    save("chat")
    # The paths of a message.
    channel, ups, snippet = PATHS_MESSAGE
    if channel != "public":
        press(text="w", ctrl=True)
        capture.settle(quiet=0.8)
        press("escape")
        typed("chat")
        press("enter")
        capture.wait_for("Public")
        capture.settle()
        typed(channel)
        press("enter")
        capture.settle(quiet=1.5)
    for _ in range(ups):
        press("up")
        time.sleep(0.15)
    capture.settle(quiet=0.6)
    for _ in range(8):
        press(text="p", ctrl=True)
        capture.settle(quiet=2.0)
        if snippet in capture.quote():
            break
        press("escape")
        capture.settle(quiet=0.6)
        press("up")
        capture.settle(quiet=0.6)
    else:
        raise TimeoutError(f"no message with {snippet!r} in {channel}")
    press("down")
    capture.settle(quiet=1.0)
    save("paths")
    # The map, from the main menu.
    press("escape")
    capture.settle(quiet=0.6)
    press(text="w", ctrl=True)
    capture.settle(quiet=0.8)
    press("escape")  # the menu may still hold the filter that the first visit typed
    typed("map")
    press("enter")
    capture.settle(quiet=4.0, timeout=120.0)
    save("map")


# --------------------------------------------------------------------------- drawing


def _draw_splash(target: Path) -> list[Path]:
    """Draw each wordmark of the splash in the emulator's font, one image pixel per pixel.

    The marks are the ANSI art that the startup splash shows, read through the same loader
    (:func:`meshterm.ui.logo.load_logo`, widest first). The emulator's raster draws them
    in Terminus 6×12, the font of the handhelds. Its cell has the shape of the VGA cell
    that the art was drawn in, so the art keeps its proportions.

    Returns:
        The paths of the images, widest mark first.
    """
    from PIL import Image

    from meshterm.emulator.font import CELL_H, CELL_W, find_font
    from meshterm.emulator.raster import Raster
    from meshterm.emulator.vt import Terminal
    from meshterm.ui.logo import load_logo, logo_width

    target.mkdir(parents=True, exist_ok=True)
    font = find_font()
    written: list[Path] = []
    limit: int | None = None
    while rows := load_logo(limit):
        width = logo_width(rows)
        terminal = Terminal(width, len(rows), palette=CAMPBELL)
        terminal.feed("\r\n".join(f"{row}\x1b[0m" for row in rows))
        raster = Raster(
            terminal,
            font,
            default_fg=CAMPBELL[7],
            default_bg=CAMPBELL[0],
            width=width * CELL_W,
            height=len(rows) * CELL_H,
            top_left=True,
        )
        raster.redraw()
        size = (raster.width, raster.height)
        path = target / f"logo.{width}.png"
        Image.frombytes("RGB", size, bytes(raster.pixels)).save(path, optimize=True)
        written.append(path)
        limit = width - 1
    return written


def _draw(name: str, grid, palette):  # noqa: ANN001, ANN202 - cells, colours, an Image
    """The image of one screen, in the look of its set."""
    from meshterm.emulator.devices import DEVICES

    handheld = DEVICES.get(name)
    if handheld is not None:
        return _draw_handheld(handheld, grid, palette)
    return TerminalPainter().draw(grid)


def _draw_handheld(handheld, grid, palette):  # noqa: ANN001, ANN202
    """The display of a handheld, drawn by the emulator's raster, at three times its size."""
    from PIL import Image

    from meshterm.emulator.font import find_font
    from meshterm.emulator.raster import Raster
    from meshterm.emulator.run import default_colours
    from meshterm.emulator.vt import Terminal

    terminal = Terminal(len(grid[0]), len(grid), palette=palette)
    terminal.screen = grid
    ink, paper = default_colours(handheld)
    width, height = handheld.panel
    raster = Raster(
        terminal,
        find_font(),
        default_fg=ink,
        default_bg=paper,
        width=width,
        height=height,
        top_left=handheld.console,
        bold_is_bright=handheld.console,
    )
    raster.redraw()
    image = Image.frombytes("RGB", (width, height), bytes(raster.pixels))
    return image.resize((width * 3, height * 3), Image.Resampling.NEAREST)


#: The colours of a cell that names none in the terminal set: the Campbell scheme of
#: Windows Terminal, which the website also uses.
TERMINAL_FG = (204, 204, 204)
TERMINAL_BG = (12, 12, 12)

#: The fonts of the terminal set, in the order in which a glyph is looked for. The emoji
#: font is used only for an emoji (a wide character, or one with VS16).
TEXT_FONTS = (
    "C:/Windows/Fonts/CascadiaMono.ttf",
    "C:/Windows/Fonts/seguisym.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)
EMOJI_FONT = "C:/Windows/Fonts/seguiemj.ttf"


class TerminalPainter:
    """Draw a cell grid as a desktop terminal shows it, at twice the size of a 15 px font.

    The painter draws at four times the size, then scales down by two. Thus the edges of
    the shapes that it draws itself (braille dots, chevrons, rounded corners) are smooth.
    """

    def __init__(self, size: int = 15) -> None:
        """Load the fonts, and measure the cell."""
        from fontTools.ttLib import TTFont
        from PIL import ImageFont

        self.ss = 4  # the size of the drawing, against the size of the font
        px = size * self.ss
        self.fonts = []
        for path in TEXT_FONTS:
            if Path(path).exists():
                cmap = set(TTFont(path, lazy=True).getBestCmap())
                self.fonts.append((ImageFont.truetype(path, px), cmap))
        self.bold = ImageFont.truetype(TEXT_FONTS[0], px)
        try:
            self.bold.set_variation_by_name("Bold")
        except (OSError, ValueError):
            pass
        self.emoji = ImageFont.truetype(EMOJI_FONT, round(px * 0.82))
        self.emoji_cmap = set(TTFont(EMOJI_FONT, lazy=True).getBestCmap())
        main = self.fonts[0][0]
        ascent, descent = main.getmetrics()
        self.cw = round(main.getlength("M") / 2) * 2
        self.ch = round((ascent + descent) * 1.02 / 2) * 2
        self.baseline = (self.ch - ascent - descent) // 2 + ascent
        self.pad = self.cw

    def draw(self, grid):  # noqa: ANN001, ANN201 - cells, an Image
        """The image of ``grid``, in a palette of 256 colours.

        A palette of 256 colours, with no dither, looks the same as the full image and
        makes the file four times smaller: a terminal has few colours, and the smooth
        edges of the text need only a few steps of each.
        """
        from PIL import Image

        image = self.render(grid)
        return image.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)

    def render(self, grid):  # noqa: ANN001, ANN201 - cells, an Image
        """The image of ``grid``, in full RGB colour."""
        from PIL import Image, ImageDraw

        cols, rows = len(grid[0]), len(grid)
        width = cols * self.cw + 2 * self.pad
        height = rows * self.ch + 2 * self.pad
        image = Image.new("RGB", (width, height), TERMINAL_BG)
        draw = ImageDraw.Draw(image)
        for row, cells in enumerate(grid):
            for col, (_, style) in enumerate(cells):
                _, bg = self._colours(style)
                x = self.pad + col * self.cw
                y = self.pad + row * self.ch
                wide = col + 1 < cols and cells[col + 1][0] == ""
                span = 2 if wide else 1
                if bg != TERMINAL_BG:
                    draw.rectangle([x, y, x + self.cw * span - 1, y + self.ch - 1], fill=bg)
        for row, cells in enumerate(grid):
            for col, (char, style) in enumerate(cells):
                if not char or char == " " or style.flags & 32:  # 32: HIDDEN
                    continue
                fg, _ = self._colours(style)
                x = self.pad + col * self.cw
                y = self.pad + row * self.ch
                wide = col + 1 < cols and cells[col + 1][0] == ""
                self._glyph(image, draw, char, x, y, fg, wide, bool(style.flags & 1))
                if style.flags & 8:  # UNDERLINE
                    draw.rectangle(
                        [x, y + self.ch - self.ss * 2, x + self.cw - 1, y + self.ch - self.ss],
                        fill=fg,
                    )
        return image.resize((width // 2, height // 2), Image.Resampling.LANCZOS)

    @staticmethod
    def _colours(style) -> tuple[tuple[int, int, int], tuple[int, int, int]]:  # noqa: ANN001
        fg = style.fg or TERMINAL_FG
        bg = style.bg or TERMINAL_BG
        if style.flags & 16:  # REVERSE
            fg, bg = bg, fg
        if style.flags & 2:  # DIM
            fg = tuple((a + b) // 2 for a, b in zip(fg, bg, strict=True))
        return fg, bg

    def _glyph(self, image, draw, char, x, y, fg, wide, bold) -> None:  # noqa: ANN001
        base = char[0]
        code = ord(base)
        if 0x2800 <= code <= 0x28FF:
            self._braille(draw, code - 0x2800, x, y, fg)
            return
        if 0x2580 <= code <= 0x259F and self._block(draw, code, x, y, fg):
            return
        if 0x2500 <= code <= 0x257F and self._box(draw, code, x, y, fg):
            return
        if 0xE0B0 <= code <= 0xE0B7 and self._powerline(draw, code, x, y, fg):
            return
        emoji = (wide or "\ufe0f" in char) and code in self.emoji_cmap
        if emoji:
            span = self.cw * 2
            draw.text(
                (x + span / 2, y + self.ch / 2), base, font=self.emoji, anchor="mm",
                embedded_color=True,
            )  # fmt: skip
            return
        font = self.bold if bold and code in self.fonts[0][1] else None
        if font is None:
            font = next((f for f, cmap in self.fonts if code in cmap), None)
        if font is None:
            # No font has the character. Draw its compatibility form, if a font has that: a
            # full-width form (the "＃" of a hashtag channel) as its ASCII character, centred
            # in its two cells. A terminal gets the same shape from a CJK font.
            folded = unicodedata.normalize("NFKC", base)
            if len(folded) == 1 and any(ord(folded) in cmap for _, cmap in self.fonts):
                char, code = folded, ord(folded)
            font = next((f for f, cmap in self.fonts if code in cmap), self.fonts[0][0])
        span = self.cw * (2 if wide else 1)
        draw.text((x + span / 2, y + self.baseline), char, font=font, fill=fg, anchor="ms")

    def _braille(self, draw, bits: int, x: int, y: int, fg) -> None:  # noqa: ANN001
        """The eight dots of a braille cell, as round dots on a 2×4 grid."""
        order = ((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3))
        radius = self.cw * 0.14
        for bit, (dx, dy) in enumerate(order):
            if bits & (1 << bit):
                cx = x + self.cw * (0.28 + 0.44 * dx)
                cy = y + self.ch * (0.125 + 0.25 * dy)
                draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=fg)

    def _block(self, draw, code: int, x: int, y: int, fg) -> bool:  # noqa: ANN001
        """A block element as filled rectangles. ``False`` for one that the font draws."""
        w, h = self.cw, self.ch
        boxes: list[tuple[float, float, float, float]] = []
        if code == 0x2580:
            boxes = [(0, 0, 1, 0.5)]
        elif 0x2581 <= code <= 0x2588:
            boxes = [(0, 1 - (code - 0x2580) / 8, 1, 1)]
        elif 0x2589 <= code <= 0x258F:
            boxes = [(0, 0, (0x2590 - code) / 8, 1)]
        elif code == 0x2590:
            boxes = [(0.5, 0, 1, 1)]
        elif code == 0x2594:
            boxes = [(0, 0, 1, 1 / 8)]
        elif code == 0x2595:
            boxes = [(7 / 8, 0, 1, 1)]
        elif 0x2596 <= code <= 0x259F:
            quads = {
                0x2596: "3", 0x2597: "4", 0x2598: "1", 0x2599: "134", 0x259A: "14",
                0x259B: "123", 0x259C: "124", 0x259D: "2", 0x259E: "23", 0x259F: "234",
            }  # fmt: skip
            place = {"1": (0, 0), "2": (0.5, 0), "3": (0, 0.5), "4": (0.5, 0.5)}
            boxes = [(*place[q], place[q][0] + 0.5, place[q][1] + 0.5) for q in quads[code]]
        elif 0x2591 <= code <= 0x2593:
            shade = (code - 0x2590) / 4
            colour = tuple(
                round(c * shade + b * (1 - shade)) for c, b in zip(fg, TERMINAL_BG, strict=True)
            )
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill=colour)
            return True
        else:
            return False
        for x0, y0, x1, y1 in boxes:
            draw.rectangle([x + x0 * w, y + y0 * h, x + x1 * w - 1, y + y1 * h - 1], fill=fg)
        return True

    #: The light, heavy, and rounded box glyphs: which of the four arms each one has
    #: (left, right, up, down), with 1 for a light arm and 2 for a heavy arm.
    _ARMS: dict[int, tuple[int, int, int, int]] = {
        0x2500: (1, 1, 0, 0), 0x2501: (2, 2, 0, 0), 0x2502: (0, 0, 1, 1), 0x2503: (0, 0, 2, 2),
        0x250C: (0, 1, 0, 1), 0x250F: (0, 2, 0, 2), 0x2510: (1, 0, 0, 1), 0x2513: (2, 0, 0, 2),
        0x2514: (0, 1, 1, 0), 0x2517: (0, 2, 2, 0), 0x2518: (1, 0, 1, 0), 0x251B: (2, 0, 2, 0),
        0x251C: (0, 1, 1, 1), 0x2523: (0, 2, 2, 2), 0x2524: (1, 0, 1, 1), 0x252B: (2, 0, 2, 2),
        0x252C: (1, 1, 0, 1), 0x2533: (2, 2, 0, 2), 0x2534: (1, 1, 1, 0), 0x253B: (2, 2, 2, 0),
        0x253C: (1, 1, 1, 1), 0x254B: (2, 2, 2, 2), 0x2574: (1, 0, 0, 0), 0x2575: (0, 0, 1, 0),
        0x2576: (0, 1, 0, 0), 0x2577: (0, 0, 0, 1),
    }  # fmt: skip

    def _box(self, draw, code: int, x: int, y: int, fg) -> bool:  # noqa: ANN001
        """A box glyph as lines through the centre of the cell, joined to its neighbours."""
        w, h = self.cw, self.ch
        light = max(self.ss, round(w / 9))
        cx, cy = x + w // 2, y + h // 2
        if code in (0x256D, 0x256E, 0x256F, 0x2570):
            # A rounded corner: a quarter circle that ends at the middle of two edges.
            r = w // 2
            corner = {
                0x256D: (cx, cy, 180),  # ╭: the arc opens to the right and down
                0x256E: (cx - 2 * r, cy, 270),  # ╮
                0x256F: (cx - 2 * r, cy - 2 * r, 0),  # ╯
                0x2570: (cx, cy - 2 * r, 90),  # ╰
            }[code]
            left, top, start = corner
            draw.arc(
                [left, top, left + 2 * r, top + 2 * r], start, start + 90, fill=fg, width=light
            )
            if code in (0x256D, 0x256E):
                draw.rectangle(
                    [cx - light // 2, cy + r, cx + light - light // 2 - 1, y + h], fill=fg
                )
            else:
                draw.rectangle([cx - light // 2, y, cx + light - light // 2 - 1, cy - r], fill=fg)
            return True
        if code in (0x2550, 0x2551):
            gap = light * 2
            for offset in (-gap, gap):
                if code == 0x2550:
                    draw.rectangle(
                        [x, cy + offset - light // 2, x + w, cy + offset + light // 2], fill=fg
                    )
                else:
                    draw.rectangle(
                        [cx + offset - light // 2, y, cx + offset + light // 2, y + h], fill=fg
                    )
            return True
        arms = self._ARMS.get(code)
        if arms is None:
            return False
        left, right, up, down = arms
        for weight, box in (
            (left, (x, cy, cx, cy)),
            (right, (cx, cy, x + w, cy)),
            (up, (cx, y, cx, cy)),
            (down, (cx, cy, cx, y + h)),
        ):
            if not weight:
                continue
            t = light * weight
            x0, y0, x1, y1 = box
            draw.rectangle(
                [x0 - t // 2, y0 - t // 2, x1 + t - t // 2 - 1, y1 + t - t // 2 - 1], fill=fg
            )
        return True

    def _powerline(self, draw, code: int, x: int, y: int, fg) -> bool:  # noqa: ANN001
        """A Powerline separator, as the shape that fills the cell."""
        w, h = self.cw, self.ch
        light = max(self.ss, round(w / 9))
        if code == 0xE0B0:
            draw.polygon([(x, y), (x + w, y + h / 2), (x, y + h)], fill=fg)
        elif code == 0xE0B2:
            draw.polygon([(x + w, y), (x, y + h / 2), (x + w, y + h)], fill=fg)
        elif code == 0xE0B1:
            draw.line([(x, y), (x + w, y + h / 2), (x, y + h)], fill=fg, width=light)
        elif code == 0xE0B3:
            draw.line([(x + w, y), (x, y + h / 2), (x + w, y + h)], fill=fg, width=light)
        elif code == 0xE0B4:
            draw.pieslice([x - w, y, x + w, y + h], 270, 90, fill=fg)
        elif code == 0xE0B6:
            draw.pieslice([x, y, x + 2 * w, y + h], 90, 270, fill=fg)
        else:
            return False
        return True


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
