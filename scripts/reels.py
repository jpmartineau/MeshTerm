# SPDX-License-Identifier: Apache-2.0
"""Make the videos of MeshTerm for Instagram from the demo mesh.

    python scripts/reels.py NAME [--out DIR] [--recording FILE]

Each video shows one feature of MeshTerm. ``NAME`` is one of :data:`REELS`. The video is
a Reel: a vertical video of 1080×1920 pixels (9:16), at 30 video frames each second. It
has subtitles in the picture and no sound. Many people watch Instagram with the sound
off, and Instagram cannot show a subtitle file on a Reel.

The script runs MeshTerm with ``--demo`` in this process, as ``scripts/video.py`` does.
The terminal has 72 columns, the narrowest terminal where each screen of the regular
platform stays readable. Thus the text is as large as the width of a phone lets it be.
Above the terminal is the name of the series (``MeshTerm · Chat``), and under it are the
subtitles. The bottom of the video stays empty, because Instagram puts the name of the
account and the caption of the post there. The video ends on a card with the wordmark
and the address of the website.

The demo device plays no background traffic. Thus the screen does not change while the
tour waits off camera for a live message, and the cut does not show.

The script writes three files into ``DIR`` (by default ``output/reels``, which git does
not track):

* ``NAME.mp4``: the video, H.264 High in ``yuv420p``, with its ``moov`` box at the
  start. It has a silent AAC track: the track costs almost nothing, and some apps that
  edit or post a video may expect one.
* ``NAME-cover.png``: the cover of the Reel, 1080×1920. The grid of a profile shows its
  centre, at 3:4.
* ``NAME.txt``: a caption for the post.

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
from dataclasses import dataclass
from pathlib import Path

import screenshots
import video
from screenshots import ROOT, Capture

FPS = video.FPS

#: The size of the video: a Reel, 9:16.
WIDTH, HEIGHT = 1080, 1920

#: The terminal: 72 columns, the narrowest one where each screen stays readable. With the
#: cell of :data:`FONT_SIZE`, it is the full width of the video.
GRID = (72, 28)

#: The size of the font of the terminal painter. It draws a cell of 14×29 pixels.
FONT_SIZE = 12

#: The part of the video that Instagram does not cover: its top bar is above, and the
#: name of the account and the caption of the post are below.
SAFE_TOP, SAFE_BOTTOM = 220, 1480

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
    """Record the tour of one video in a child process, then paint and encode it."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", choices=sorted(REELS))
    parser.add_argument("--out", type=Path, default=ROOT / "output" / "reels")
    parser.add_argument(
        "--recording",
        type=Path,
        help="keep the recording in this file. If the file exists, use it and do not run MeshTerm",
    )
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    reel = REELS[args.name]
    if args.child:
        video._watch_live_start()
        return screenshots.run_demo(
            "terminal",
            lambda capture, keys: _record(capture, keys, reel, args.child),
            background=False,
            grid=GRID,
        )
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meshterm-reel-") as work:
        recording = args.recording or Path(work) / "recording.pickle"
        if not recording.exists():
            print(f"recording the tour of {args.name}...", flush=True)
            home = Path(tempfile.gettempdir()) / "meshterm-reels"
            done = subprocess.run(
                [sys.executable, __file__, args.name, "--child", str(recording)],
                env=screenshots.child_env(home),
                check=False,
            )
            if done.returncode or not recording.exists():
                print(f"the recording failed ({done.returncode})", flush=True)
                return 1
        tape = pickle.loads(recording.read_bytes())
        states = _states(tape["frames"], tape["cues"])
        print(f"painting {len(set(states))} images for {len(states) / FPS:.1f} s...", flush=True)
        images = _paint(reel.title, tape["grids"], tape["cues"], states, Path(work))
        target = args.out / f"{args.name}.mp4"
        print("encoding...", flush=True)
        _encode([images[state] for state in states], Path(work), target)
        frames = tape["frames"]
        mark = tape["marks"].get("poster", len(frames) // 2)
        poster = tape["grids"][frames[min(mark, len(frames) - 1)]]
        cover = args.out / f"{args.name}-cover.png"
        Composer(reel.title).frame(poster, (tape["cues"][0][1], 1.0, True)).save(cover)
        caption = args.out / f"{args.name}.txt"
        caption.write_text(reel.caption, encoding="utf-8")
    for path in (target, cover, caption):
        print(f"{path} ({path.stat().st_size // 1024} KB)", flush=True)
    return 0


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


def _chat(d: Director) -> None:
    """The chat: the conversations, a live message, the paths, a mention, and a QR code."""
    from meshterm.demo import script

    capture = d.capture
    capture.wait_for("Chat", timeout=60)
    capture.settle(quiet=0.5)
    # The video starts on the list of the conversations. The main menu is not in it.
    d.type("chat")
    d.key("enter")
    d.wait_for("pick a conversation")
    capture.settle(quiet=0.8)

    print("the conversations", flush=True)
    d.subtitle("Off-grid chat over *LoRa radio*,\nright in your terminal")
    d.camera.roll()
    d.hold(2.8)
    d.type("pub")
    d.hold(0.5)
    d.key("enter", pause=0.0)
    d.wait_for(screenshots.PATHS_MESSAGE[2])
    d.subtitle("Everyone gets\ntheir own *colour*")
    d.hold(2.6)

    print("a live message", flush=True)
    offset, _, line = next(item for item in script.LIVE_CHANNEL if item[1] == "Public")
    with d.off_camera():
        d.wait_until(video._start() + offset - 0.8)
    d.wait_for(line.text, timeout=20)
    d.subtitle("New messages arrive *live*,\nover the air")
    d.hold(2.4)

    print("the paths of a message", flush=True)
    for _ in range(3):  # Chloé, Félix, then Gabriel
        d.key("up", pause=0.45)
    d.hold(0.5)
    # A key press can be lost while the transcript draws. The camera does not show the
    # tries: it shows the dialog of the right message as it opens.
    with d.off_camera():
        video._open_paths(d)
    d.subtitle("See *every path* a message\ntook to reach you")
    d.hold(2.2)
    d.key("down", pause=1.4)
    d.key("up", pause=1.4)
    d.key("down", pause=0.5)
    d.camera.mark("poster")
    d.hold(1.6)
    d.key("escape")
    d.key("escape")  # the first Esc closes the dialog, the second clears the selection

    print("a mention, and its answer", flush=True)
    d.subtitle("*Mentions* turn into\nname tags as you type")
    d.hold(0.6)
    # The mention is typed in full: the ] makes it a chip.
    d.type("Hey @[Dave-Lachine], are you coming Saturday?")
    d.hold(0.8)
    d.key("enter", pause=0.0)
    sent = time.monotonic()
    d.wait_for("0/130")  # the compose line is empty: the message went
    d.hold(0.8)
    # Dave answers after ``script.REPLY_DELAY_S``. The camera does not show most of the wait.
    with d.off_camera():
        d.wait_until(sent + script.REPLY_DELAY_S - 0.6)
    d.wait_for("be there at 2", timeout=30)
    d.subtitle("When someone *mentions you*,\nyou get a ★")
    d.hold(2.8)

    print("a link as a QR code", flush=True)
    d.subtitle("Turn any link into a\n*QR code* for your phone")
    d.hold(0.4)
    d.walk_to("11:04")  # our message with the link to the map
    d.hold(0.7)
    d.key(text="u", ctrl=True, pause=0.0)
    d.hold(3.4)


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

        # The block from the series line to the subtitles, in the middle of the safe part.
        tw, th = self.term_size
        block = 44 + 36 + th + 44 + 2 * self.line_height
        top = SAFE_TOP + (SAFE_BOTTOM - SAFE_TOP - block) // 2
        self.term_at = ((WIDTH - tw) // 2, top + 44 + 36)
        self.sub_top = self.term_at[1] + th + 44

        self.base = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
        draw = ImageDraw.Draw(self.base)
        self._spans(draw, [("MeshTerm", BRAND), (f" · {title}", MUTED)], top + 36, self.title_font)
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
        top = SAFE_TOP + (SAFE_BOTTOM - SAFE_TOP - block) // 2
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


# --------------------------------------------------------------------------- the reels


@dataclass(frozen=True)
class Reel:
    """One video of the series.

    Attributes:
        title: The name of the feature, in the series line above the terminal.
        tour: The keys of the tour, and the subtitles.
        caption: The text of the post on Instagram.
    """

    title: str
    tour: Callable[[Director], None]
    caption: str


REELS: dict[str, Reel] = {
    "chat": Reel(
        "Chat",
        _chat,
        "Off-grid chat over LoRa radio, right in your terminal.\n"
        "\n"
        "MeshTerm is a free, open-source MeshCore client for Windows, macOS, and Linux. "
        "Chat on channels, in direct messages, and in rooms. See every path a message took "
        "across the mesh. Mention a friend, and their name turns into a tag. Turn any link "
        "into a QR code for your phone.\n"
        "\n"
        "Filmed in MeshTerm's demo mesh over Montréal (meshterm --demo).\n"
        "\n"
        "meshterm.net\n"
        "\n"
        "#MeshCore #LoRa #OffGrid #MeshNetwork #Terminal #TUI #OpenSource\n",
    ),
}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
