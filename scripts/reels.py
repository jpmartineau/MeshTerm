# SPDX-License-Identifier: Apache-2.0
"""Make the videos of MeshTerm for Instagram from the demo mesh.

    python scripts/reels.py NAME ... [--out DIR] [--recordings DIR]
    python scripts/reels.py all [--out DIR] [--recordings DIR]

Each video shows one page of MeshTerm. ``NAME`` is a key of ``reel_tours.REELS``, where
the tours are, and ``all`` makes each video, one after the other. The video is a Reel: a
vertical video of 1080×1920 pixels (9:16), at 30 video frames each second. It has
subtitles in the picture and no sound. Many people watch Instagram with the sound off,
and Instagram cannot show a subtitle file on a Reel.

The script runs MeshTerm with ``--demo`` in this process, as ``scripts/video.py`` does.
The terminal has 72 columns, the narrowest terminal where each screen of the regular
platform stays readable. Thus the text is as large as the width of a phone lets it be.
Above the terminal is the name of the series (``MeshTerm for MeshCore · Chat``), and
under it are the subtitles. The bottom of the video stays empty, because Instagram puts
the name of the account and the caption of the post there. The video ends on a card with
the wordmark and the address of the website.

The demo device plays no background traffic, except for a page that shows the traffic
(``Reel.background``). Thus the screen does not change while the tour waits off camera
for a live message, and the cut does not show.

For each video, the script writes three files into ``DIR`` (by default ``output/reels``,
which git does not track):

* ``NAME.mp4``: the video, H.264 High in ``yuv420p``, with its ``moov`` box at the
  start. It has a silent AAC track: the track costs almost nothing, and some apps that
  edit or post a video may expect one.
* ``NAME-cover.png``: the cover of the Reel, 1080×1920. The grid of a profile shows its
  centre, at 3:4.
* ``NAME.txt``: a caption for the post.

With ``--recordings``, the script keeps the recording of each video in that folder
(``NAME.pickle``). When the file exists, a later run paints and encodes it again, and does
not run MeshTerm. Never keep a recording across a change to the app.

What it needs: the needs of ``scripts/video.py``, but not the website checkout.
"""

from __future__ import annotations

import argparse
import pickle
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import screenshots
import video
from reel_tours import REELS, Reel
from screenshots import ROOT, Capture

FPS = video.FPS

#: The size of the video: a Reel, 9:16.
WIDTH, HEIGHT = 1080, 1920

#: The terminal: 72 columns, the narrowest one where each screen stays readable. With the
#: cell of :data:`FONT_SIZE`, it is the full width of the video. Its 38 rows fill the
#: height of the video down to the subtitles (refer to :data:`TOP`).
GRID = (72, 38)

#: The size of the font of the terminal painter. It draws a cell of 14×29 pixels.
FONT_SIZE = 12

#: The part of the video that the series line, the terminal, and the subtitles fill.
#: Instagram's top bar is above it. The name of the account and the caption of the post
#: are below it, in the bottom 20%. Some guides keep the bottom 35% free, but then the
#: terminal fills less than half of the height. With this layout, a long caption can
#: cover the subtitles, but not the terminal (JP chose this layout, 2026-10-17).
TOP, BOTTOM = 150, 1530

#: The colours. ``INK`` is the bright white of the website, ``MUTED`` the muted grey of
#: the theme, and ``BORDER`` the dark grey of the theme.
BLACK = (0, 0, 0)
INK = (242, 242, 242)
SOFT = (204, 204, 204)
MUTED = (162, 162, 162)
BORDER = (64, 64, 64)
BRAND = video.BRAND

#: The fade of a subtitle, in video frames: the old one goes out, then the new one comes in
#: and moves up by :data:`SLIDE` pixels.
FADE_OUT, FADE_IN, SLIDE = 4, 8, 14

#: The end card: the fade to it, and the time that it stays, in video frames.
CARD_FADE, CARD_HOLD = 15, int(3.5 * FPS)

#: The wordmark of the splash, which the end card shows.
LOGO = ROOT / "docs" / "screenshots" / "splash" / "logo.71.png"

#: A subtitle cue: the video frame where it starts, and its text. ``*text*`` is in the
#: brand colour, and ``\n`` starts the second line. ``None`` clears the subtitle.
Cue = tuple[int, str | None]


def main(argv: list[str]) -> int:
    """Make each video that ``argv`` names, one after the other.

    Returns:
        0 if each video was made, 1 if one or more failed. A failure does not stop the
        videos after it.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="+", metavar="NAME", help=f"all, or: {', '.join(REELS)}")
    parser.add_argument("--out", type=Path, default=ROOT / "output" / "reels")
    parser.add_argument(
        "--recordings",
        type=Path,
        help="keep each recording in this folder. If it has one, use it and do not run MeshTerm",
    )
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    names = list(REELS) if args.names == ["all"] else args.names
    unknown = [name for name in names if name not in REELS]
    if unknown:
        parser.error(f"no video named {', '.join(unknown)}")
    if args.child:
        reel = REELS[names[0]]
        video._watch_live_start()
        return screenshots.run_demo(
            "terminal",
            lambda capture, keys: _record(capture, keys, reel, args.child),
            background=reel.background,
            grid=GRID,
        )
    args.out.mkdir(parents=True, exist_ok=True)
    if args.recordings:
        args.recordings.mkdir(parents=True, exist_ok=True)
    failed = [name for name in names if not _make(name, args.out, args.recordings)]
    if failed:
        print(f"failed: {', '.join(failed)}", flush=True)
    return 1 if failed else 0


def _make(name: str, out: Path, recordings: Path | None) -> bool:
    """Record the tour of one video in a child process, then paint and encode it.

    Returns:
        Whether the video was made.
    """
    reel = REELS[name]
    with tempfile.TemporaryDirectory(prefix="meshterm-reel-") as work:
        recording = (recordings or Path(work)) / f"{name}.pickle"
        if not recording.exists():
            print(f"{name}: recording the tour...", flush=True)
            home = Path(tempfile.gettempdir()) / "meshterm-reels"
            done = subprocess.run(
                [sys.executable, __file__, name, "--child", str(recording)],
                # The size of the terminal for what reads it from the environment, such as
                # Diagnostics (shutil.get_terminal_size). The process has no terminal.
                env={
                    **screenshots.child_env(home),
                    "COLUMNS": str(GRID[0]),
                    "LINES": str(GRID[1]),
                },
                check=False,
            )
            if done.returncode or not recording.exists():
                print(f"{name}: the recording failed ({done.returncode})", flush=True)
                return False
        tape = pickle.loads(recording.read_bytes())
        states = _states(tape["frames"], tape["cues"])
        print(
            f"{name}: painting {len(set(states))} images for {len(states) / FPS:.1f} s...",
            flush=True,
        )
        images = _paint(reel.title, tape["grids"], tape["cues"], states, Path(work))
        target = out / f"{name}.mp4"
        _encode([images[state] for state in states], Path(work), target)
        frames = tape["frames"]
        mark = tape["marks"].get("poster", len(frames) // 2)
        poster = tape["grids"][frames[min(mark, len(frames) - 1)]]
        cover = out / f"{name}-cover.png"
        Composer(reel.title).frame(poster, (tape["cues"][0][1], 1.0, True)).save(cover)
        caption = out / f"{name}.txt"
        caption.write_text(reel.caption, encoding="utf-8")
    for path in (target, cover, caption):
        print(f"{path} ({path.stat().st_size // 1024} KB)", flush=True)
    return True


# --------------------------------------------------------------------------- the child


class Director(video.Presenter):
    """The person at the keyboard, who also writes the subtitles."""

    def __init__(self, capture: Capture, type_text: Callable[[str], None], camera) -> None:  # noqa: ANN001 - a Recorder
        """Keep the capture, the input of MeshTerm, and the camera."""
        super().__init__(capture, type_text, camera)
        self.cues: list[Cue] = []

    def subtitle(self, text: str | None) -> None:
        """Show ``text`` from the next video frame (refer to :data:`Cue`)."""
        self.cues.append((len(self.camera.frames), text))

    def open_page(
        self, name: str, wait: str, *, settle: float = 0.8, timeout: float = 30.0
    ) -> None:
        """Open a page from the main menu, off camera: type ``name`` to filter, then Enter.

        Args:
            name: The start of the name of the page in the menu.
            wait: A text that the page shows when it is open.
            settle: How long the screen must stay the same before the tour goes on.
            timeout: How long to wait for the page (the map loads its tiles).
        """
        self.capture.wait_for("Chat", timeout=60)
        self.capture.settle(quiet=0.5)
        self.type(name)
        self.key("enter")
        self.wait_for(wait, timeout=timeout)
        self.capture.settle(quiet=settle, timeout=timeout)

    def start(self, text: str) -> None:
        """Show the first subtitle, and roll the camera."""
        self.subtitle(text)
        self.camera.roll()

    def count(self, text: str) -> int:
        """How many times ``text`` is on the screen now."""
        with self.capture.lock:
            return self.capture.terminal.text().count(text)

    def wait_until_shows(self, test: Callable[[], bool], timeout: float = 30.0) -> None:
        """Wait until ``test`` is true.

        Raises:
            TimeoutError: The test stayed false.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if test():
                return
            time.sleep(0.1)
        with self.capture.lock:
            screen = self.capture.terminal.text()
        raise TimeoutError(f"the screen did not change as expected. The screen:\n{screen}")

    def walk_to(self, label: str, key: str = "up", tries: int = 16, pause: float = 0.22) -> None:
        """Press ``key`` at a steady pace until the highlight is on the line of ``label``.

        Raises:
            TimeoutError: The highlight did not come to the line.
        """
        for _ in range(tries):
            if self._highlighted(label):
                return
            self.key(key, pause=pause)
        if not self._highlighted(label):
            raise TimeoutError(f"the highlight did not come to {label!r}")


def _record(capture: Capture, type_text: Callable[[str], None], reel: Reel, path: Path) -> None:
    """Record the tour of ``reel``, and write the recording to ``path``."""
    camera = video.Recorder(capture)
    director = Director(capture, type_text, camera)
    reel.tour(director)
    camera.cut()
    camera.stop()
    tape = {
        "grids": camera.grids,
        "frames": camera.frames,
        "marks": camera.marks,
        "cues": director.cues,
    }
    path.write_bytes(pickle.dumps(tape, protocol=pickle.HIGHEST_PROTOCOL))
    print(f"recorded {len(camera.frames) / FPS:.1f} s, {len(camera.grids)} screens", flush=True)


# --------------------------------------------------------------------------- the video

#: The state of one video frame: the copy of the screen and the subtitle, or the end card.
#: A subtitle is ``(cue, level, entering)``: the index of its cue, how far it faded in or
#: out (``0`` to ``FADE_IN`` or ``FADE_OUT``), and whether it comes in. ``("card", step)`` is
#: a step of the fade to the end card, and ``CARD_FADE`` is the card alone.
State = tuple


def _states(frames: list[int], cues: list[Cue]) -> list[State]:
    """The state of each video frame, with the end card after the last one."""
    starts = [start for start, _ in cues]
    states: list[State] = []
    current = -1
    for frame, grid in enumerate(frames):
        while current + 1 < len(cues) and starts[current + 1] <= frame:
            current += 1
        states.append(("tour", grid, _subtitle_state(frame, current, cues)))
    last = states[-1]
    states += [("card", step, last) for step in range(1, CARD_FADE)]
    states += [("card", CARD_FADE, None)] * CARD_HOLD
    return states


def _subtitle_state(frame: int, current: int, cues: list[Cue]) -> tuple | None:
    """The subtitle of a video frame (refer to :data:`State`), or ``None``."""
    if current < 0:
        return None
    start, text = cues[current]
    age = frame - start
    previous = cues[current - 1][1] if current else None
    if previous is not None and age < FADE_OUT:
        return (current - 1, FADE_OUT - age, False)
    if text is None:
        return None
    if current == 0 and start == 0:
        return (current, FADE_IN, True)  # the first subtitle is there from the start
    age -= FADE_OUT if previous is not None else 0
    return (current, min(FADE_IN, age + 1), True)


class Composer:
    """Draw one video frame: the series line, the terminal, and the subtitle."""

    def __init__(self, title: str) -> None:
        """Load the fonts, measure the layout, and draw the parts that never change."""
        from fontTools.ttLib import TTFont
        from PIL import Image, ImageDraw, ImageFont

        self.painter = screenshots.TerminalPainter(FONT_SIZE)
        cols, rows = GRID
        p = self.painter
        self.term_size = ((cols * p.cw + 2 * p.pad) // 2, (rows * p.ch + 2 * p.pad) // 2)
        mono = screenshots.TEXT_FONTS[0]
        self.cmap = set(TTFont(mono, lazy=True).getBestCmap())
        self.sub_font = self._bold(mono, 48)
        self.title_font = self._bold(mono, 40)
        self.symbol_font = ImageFont.truetype(screenshots.TEXT_FONTS[1], 46)
        self.advance = self.sub_font.getlength("M")
        self.line_height = 64

        # The block from the series line to the subtitles, in the middle of its part.
        tw, th = self.term_size
        block = 44 + 36 + th + 44 + 2 * self.line_height
        top = TOP + (BOTTOM - TOP - block) // 2
        self.term_at = ((WIDTH - tw) // 2, top + 44 + 36)
        self.sub_top = self.term_at[1] + th + 44

        self.base = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
        draw = ImageDraw.Draw(self.base)
        series = [("MeshTerm", BRAND), (" for MeshCore", SOFT), (f" · {title}", MUTED)]
        self._spans(draw, series, top + 36, self.title_font)
        self.mask = Image.new("L", self.term_size, 0)
        ImageDraw.Draw(self.mask).rounded_rectangle([0, 0, tw - 1, th - 1], radius=18, fill=255)

    @staticmethod
    def _bold(path: str, size: int):  # noqa: ANN205 - a FreeTypeFont
        from PIL import ImageFont

        font = ImageFont.truetype(path, size)
        try:
            font.set_variation_by_name("Bold")
        except (OSError, ValueError):
            pass
        return font

    def frame(self, grid, subtitle: tuple[str | None, float, bool] | None):  # noqa: ANN001, ANN201
        """The video frame of ``grid``, with ``(text, level, entering)`` as its subtitle.

        ``level`` goes from 0 (not visible) to 1 (all visible).
        """
        return self.compose(self.painter.render(grid), subtitle)

    def compose(self, terminal, subtitle: tuple[str | None, float, bool] | None):  # noqa: ANN001, ANN201
        """The video frame of a painted terminal (refer to :meth:`frame`)."""
        from PIL import Image, ImageDraw

        image = self.base.copy()
        image.paste(terminal, self.term_at, self.mask)
        x, y = self.term_at
        tw, th = self.term_size
        ImageDraw.Draw(image).rounded_rectangle(
            [x - 1, y - 1, x + tw, y + th], radius=19, outline=BORDER, width=2
        )
        if subtitle is not None and subtitle[0] and subtitle[1] > 0:
            text, level, entering = subtitle
            height = 2 * self.line_height + SLIDE + 16
            layer = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(layer)
            ease = 1 - (1 - level) ** 3
            shift = round((1 - ease) * SLIDE) if entering else 0
            alpha = round(255 * level)
            for row, line in enumerate(text.split("\n")):
                baseline = shift + 48 + row * self.line_height
                self._spans(draw, _markup(line), baseline, self.sub_font, alpha)
            region = (0, self.sub_top, WIDTH, self.sub_top + height)
            crop = image.crop(region).convert("RGBA")
            image.paste(Image.alpha_composite(crop, layer).convert("RGB"), region[:2])
        return image

    def _spans(self, draw, spans, baseline: float, font, alpha: int = 255) -> None:  # noqa: ANN001
        """Draw coloured spans of text as one centred line of the monospace font.

        A character that the font does not have (``★``) comes from the symbol font, centred
        in its cell.
        """
        advance = font.getlength("M")
        count = sum(len(text) for text, _ in spans)
        x = (WIDTH - count * advance) / 2
        for text, colour in spans:
            for char in text:
                face = font if ord(char) in self.cmap else self.symbol_font
                draw.text(
                    (x + advance / 2, baseline), char, font=face, fill=(*colour, alpha), anchor="ms"
                )
                x += advance

    def card(self):  # noqa: ANN201 - an Image
        """The end card: the wordmark, what MeshTerm is, and the address of the website."""
        from PIL import Image, ImageDraw

        logo = Image.open(LOGO).convert("RGB")
        # The black of the art is the Campbell black. The video is pure black.
        pixels = logo.load()
        for y in range(logo.height):
            for x in range(logo.width):
                if pixels[x, y] == screenshots.CAMPBELL[0]:
                    pixels[x, y] = BLACK
        logo = logo.resize((logo.width * 2, logo.height * 2), Image.Resampling.NEAREST)
        lines = [
            (self.title_font, [("A full-featured MeshCore client", INK)], 58),
            (self.title_font, [("for your terminal", INK)], 82),
            (self.title_font, [("Free and open source", MUTED)], 58),
            (self.title_font, [("Windows, macOS, and Linux", MUTED)], 110),
            (self._bold(screenshots.TEXT_FONTS[0], 64), [("meshterm.net", BRAND)], 0),
        ]
        block = logo.height + 90 + sum(gap for *_, gap in lines) + 40
        top = TOP + (BOTTOM - TOP - block) // 2
        image = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
        image.paste(logo, ((WIDTH - logo.width) // 2, top))
        draw = ImageDraw.Draw(image)
        baseline = top + logo.height + 90 + 40
        for font, spans, gap in lines:
            self._spans(draw, spans, baseline, font)
            baseline += gap
        return image


def _markup(line: str) -> list[tuple[str, tuple[int, int, int]]]:
    """The coloured spans of one subtitle line: ``*text*`` is in the brand colour."""
    parts = line.split("*")
    return [(part, BRAND if index % 2 else INK) for index, part in enumerate(parts) if part]


#: The composer of each worker process (refer to :func:`_paint`).
_composer: Composer | None = None


def _start_composer(title: str) -> None:
    global _composer
    _composer = Composer(title)


def _paint_grid(job: tuple[object, list[tuple[str, tuple | None]]]) -> None:
    """Paint one copy of the screen, and draw each of its video frames into a file."""
    grid, outputs = job
    terminal = _composer.painter.render(grid)
    for path, subtitle in outputs:
        _composer.compose(terminal, subtitle).save(path, compress_level=1)


def _paint(
    title: str, grids: list, cues: list[Cue], states: Iterable[State], folder: Path
) -> dict[State, Path]:
    """Draw each different video frame into a PNG file in ``folder``, in parallel.

    Returns:
        The path of the image of each state.
    """
    paths: dict[State, Path] = {}
    jobs: dict[int, list[tuple[str, tuple | None]]] = {}
    cards: list[State] = []
    for state in states:
        if state in paths:
            continue
        paths[state] = folder / f"{len(paths):05d}.png"
        if state[0] == "card":
            cards.append(state)
            continue
        _, grid, subtitle = state
        jobs.setdefault(grid, []).append((str(paths[state]), _resolve(subtitle, cues)))
    with ProcessPoolExecutor(initializer=_start_composer, initargs=(title,)) as pool:
        list(pool.map(_paint_grid, ((grids[g], out) for g, out in jobs.items()), chunksize=4))
    from PIL import Image

    composer = Composer(title)
    card = composer.card()
    for state in cards:
        _, step, last = state
        if last is None:
            card.save(paths[state], compress_level=1)
            continue
        before = Image.open(paths[last]).convert("RGB")
        t = step / CARD_FADE
        Image.blend(before, card, t * t * (3 - 2 * t)).save(paths[state], compress_level=1)
    return paths


def _resolve(subtitle: tuple | None, cues: list[Cue]) -> tuple[str | None, float, bool] | None:
    """The text and the level (0 to 1) of a subtitle state."""
    if subtitle is None:
        return None
    cue, level, entering = subtitle
    return (cues[cue][1], level / (FADE_IN if entering else FADE_OUT), entering)


def _encode(images: list[Path], work: Path, target: Path) -> None:
    """Encode the video frames into ``target`` with ffmpeg, with a silent audio track.

    As in :func:`video._encode`, each different image goes to ffmpeg one time, with the
    time that it stays on the screen, and the RGB to YUV conversion uses the BT.709
    matrix. Instagram encodes each upload again, so this file is made for quality, not
    for size: CRF 16 and a keyframe each two seconds.
    """
    runs: list[list] = []
    for path in images:
        if runs and runs[-1][0] == path:
            runs[-1][1] += 1
        else:
            runs.append([path, 1])
    lines = ["ffconcat version 1.0"]
    for path, count in runs:
        lines += [f"file '{path.as_posix()}'", f"duration {count / FPS:.6f}"]
    lines.append(f"file '{runs[-1][0].as_posix()}'")
    listing = work / "frames.ffconcat"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    colours = "color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709:range=tv"
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-map", "0:v", "-map", "1:a",
        "-vf", f"fps={FPS},scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
               f"setparams={colours}",
        "-t", f"{len(images) / FPS:.3f}",
        "-c:v", "libx264", "-preset", "slow", "-tune", "animation", "-crf", "16",
        "-profile:v", "high", "-level", "4.1", "-g", str(FPS * 2),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(target),
    ]  # fmt: skip
    subprocess.run(command, check=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
