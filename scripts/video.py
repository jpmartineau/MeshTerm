# SPDX-License-Identifier: Apache-2.0
"""Make the demo video of the website from the demo mesh.

    python scripts/video.py [--out DIR] [--recording FILE]
    python scripts/video.py --readme [--out DIR]

The script runs MeshTerm with ``--demo`` in an 80×24 terminal, as the terminal set of
``scripts/screenshots.py`` does, and types the keys of a tour of less than two minutes:

1. The main menu.
2. The chat on Public, and the two paths of a message from Beloeil, each selected in turn.
3. A message that arrives on Public. Then a question that mentions Dave, typed so that the
   mention becomes a chip, and his answer, which mentions our node.
4. A trace to Brossard, across the river, and back.
5. The map, from the city down to the streets and the buildings around our node.
6. Repeater admin on Mont-Royal: a read of its settings, then an advert.
7. The live feed, and one packet from it.

A recorder (the camera) copies the screen at each video frame, 30 times each second, while
it rolls. The tour stops the camera while it waits for something slow (the full read of the
settings, the tiles of the map), so the video jumps over the wait. After the tour, the
script paints each different copy in parallel, as the terminal set of the screenshots
paints a screen, and ffmpeg encodes the video.

The script writes three files into ``DIR``. By default, ``DIR`` is ``src/`` of the website
checkout beside this one (``../meshterm.net/src``), where the build of the website reads
them:

* ``demo.mp4``: the video, H.264 High in ``yuv420p``, with its ``moov`` box at the start.
  Thus a browser can start to play it before all of it arrives.
* ``demo-poster.png``: the video frame that the website shows before the video plays (the
  paths of the message).
* ``demo-play.png``: the same image with a play mark and the length of the video. A page
  that cannot play the video links to it with this image.

Each run gives the same video to the eye, but not the same file. The tour types the same
keys at the same pace, and the demo plays the same messages and packets at the same times
after its start. But the camera copies the screen on the real clock, so a change can come
one video frame earlier or later than in another run.

With ``--readme``, the script makes no video. It uploads ``DIR/demo.mp4`` to GitHub for
the README of MeshTerm, and puts the URL of the upload in ``README.md`` (refer to
:func:`_publish_readme`).

What it needs, beside the needs of ``scripts/screenshots.py``: ffmpeg, with libx264, on the
``PATH``. With ``--readme``, the GitHub CLI (``gh``), logged in to an account that can
write to MeshTerm.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import pickle
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import screenshots
from screenshots import ROOT, Capture

#: The video frames in each second.
FPS = 30

#: The default folder of the three files: ``src/`` of the website checkout.
SITE_SRC = ROOT.parent / "meshterm.net" / "src"

#: The colours of the play mark and of the length on the card: the ``brand`` and
#: ``title.brand`` teals of the theme.
BRAND = (0x5E, 0xEA, 0xD4)
BRAND_LIGHT = (0x99, 0xF6, 0xE4)

#: One copy of the screen: the rows of ``(character, style)`` cells of the VT parser.
Grid = tuple[tuple[tuple[str, object], ...], ...]

#: The repository whose README plays the video.
REPOSITORY = "jpmartineau/MeshTerm"

#: The line of the README that GitHub shows as a video player: the URL of an attachment,
#: alone on its line.
ATTACHMENT_LINE = re.compile(r"^https://github\.com/user-attachments/assets/[0-9a-f-]+$", re.M)


def main(argv: list[str]) -> int:
    """Record the tour in a child process, then paint and encode the video."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=SITE_SRC)
    parser.add_argument(
        "--recording",
        type=Path,
        help="keep the recording in this file. If the file exists, use it and do not run MeshTerm",
    )
    parser.add_argument(
        "--readme",
        action="store_true",
        help="make no video: upload DIR/demo.mp4 for the README, and put its URL in README.md",
    )
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        _watch_live_start()
        return screenshots.run_demo(
            "terminal", lambda capture, keys: _record(capture, keys, args.child), background=True
        )
    if args.readme:
        print(_publish_readme(args.out / "demo.mp4"), flush=True)
        return 0
    if not args.out.is_dir():
        raise SystemExit(f"video.py: {args.out} is not a folder. Pass --out.")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("video.py: ffmpeg is not on the PATH")
    with tempfile.TemporaryDirectory(prefix="meshterm-video-") as work:
        recording = args.recording or Path(work) / "recording.pickle"
        if not recording.exists():
            print("recording the tour...", flush=True)
            home = Path(tempfile.gettempdir()) / "meshterm-video"
            done = subprocess.run(
                [sys.executable, __file__, "--child", str(recording)],
                env=screenshots.child_env(home),
                check=False,
            )
            if done.returncode or not recording.exists():
                print(f"the recording failed ({done.returncode})", flush=True)
                return 1
        tape = pickle.loads(recording.read_bytes())
        grids, frames = tape["grids"], tape["frames"]
        seconds = len(frames) / FPS
        print(f"painting {len(grids)} screens for {seconds:.1f} s of video...", flush=True)
        paths = _paint(grids, Path(work))
        video = args.out / "demo.mp4"
        print("encoding...", flush=True)
        _encode(frames, paths, Path(work), video)
        poster = grids[frames[min(tape["marks"]["poster"], len(frames) - 1)]]
        written = [video, *_stills(poster, seconds, args.out)]
    for path in written:
        print(f"{path} ({path.stat().st_size // 1024} KB)", flush=True)
    return 0


# --------------------------------------------------------------------------- the child

#: The time on the monotonic clock at which the demo device started its live traffic,
#: or ``None`` before that. The times of the lines of the demo script count from it.
_live_start: float | None = None


def _watch_live_start() -> None:
    """Keep the time at which the demo device starts its live traffic.

    The device counts the time of each live line from the moment that the app subscribes
    to its events, on the clock of the event loop (the monotonic clock). The tour uses this
    time to start the camera a moment before a line arrives.
    """
    from meshterm.demo.device import DemoDevice

    subscribe = DemoDevice.subscribe_events

    async def subscribe_events(self, on_event):  # noqa: ANN001, ANN202 - the same as the original
        global _live_start
        if _live_start is None:
            _live_start = time.monotonic()
        return await subscribe(self, on_event)

    DemoDevice.subscribe_events = subscribe_events


class Recorder:
    """The camera: a copy of the screen at each video frame, while it rolls.

    Each different copy is kept one time (:attr:`grids`), and each video frame is the index
    of its copy (:attr:`frames`). When the camera is late, it gives the same copy to each
    video frame that it missed. Thus the video keeps the time of the real clock.
    """

    def __init__(self, capture: Capture) -> None:
        """Start the thread of the camera. It does not roll yet."""
        self.capture = capture
        self.grids: list[Grid] = []
        self.frames: list[int] = []
        self.marks: dict[str, int] = {}
        self._index: dict[Grid, int] = {}
        self._last: tuple[Grid, int] | None = None
        self._rolling = False
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def roll(self) -> None:
        """Start to record."""
        self._rolling = True

    def cut(self) -> None:
        """Stop to record. The video continues from here at the next roll."""
        self._rolling = False

    def mark(self, name: str) -> None:
        """Give the next video frame a name."""
        self.marks[name] = len(self.frames)

    def stop(self) -> None:
        """Stop the camera for good."""
        self._stopped = True
        self._thread.join()

    def _run(self) -> None:
        period = 1.0 / FPS
        due: float | None = None
        while not self._stopped:
            if not self._rolling:
                due = None
                time.sleep(period / 2)
                continue
            now = time.monotonic()
            if due is None:
                due = now
            if now < due:
                time.sleep(due - now)
                continue
            count = int((now - due) / period) + 1
            self.frames.extend([self._copy()] * count)
            due += count * period

    def _copy(self) -> int:
        """The index of the copy of the screen as it is now."""
        with self.capture.lock:
            grid = tuple(tuple(row) for row in self.capture.terminal.screen)
        # The cells that did not change are the same objects, so this test is fast.
        if self._last is not None and grid == self._last[0]:
            return self._last[1]
        index = self._index.get(grid)
        if index is None:
            index = self._index[grid] = len(self.grids)
            self.grids.append(grid)
        self._last = (grid, index)
        return index


class Presenter:
    """The person at the keyboard: key presses at a human pace, and the camera."""

    def __init__(self, capture: Capture, type_text: Callable[[str], None], camera: Recorder):
        """Keep the capture, the input of MeshTerm, and the camera."""
        self.capture = capture
        self.camera = camera
        self._type_text = type_text
        # A fixed seed: the pace of the typing is the same at each run.
        self._rng = random.Random("meshterm-video")

    def key(self, name: str = "", text: str = "", *, ctrl: bool = False, pause: float = 0.35):
        """Press one keyboard key, then wait for ``pause`` seconds.

        After Esc, the wait is at least 0.7 s. prompt_toolkit waits a moment to see if an
        Esc starts a longer sequence, and a key that comes in that time joins the Esc.
        """
        from meshterm.emulator.keys import Key, encode

        self._type_text(encode(Key(name=name, text=text, ctrl=ctrl)))
        time.sleep(max(pause, 0.7) if name == "escape" else pause)

    def type(self, text: str) -> None:
        """Type ``text``, one character at a time, at the pace of a person."""
        for char in text:
            self._type_text(char)
            time.sleep(self._rng.uniform(0.06, 0.14))

    def hold(self, seconds: float) -> None:
        """Wait, while the camera records what is on the screen."""
        time.sleep(seconds)

    def wait_for(self, text: str, timeout: float = 30.0) -> None:
        """Wait until ``text`` is on the screen."""
        self.capture.wait_for(text, timeout)

    def wait_until(self, moment: float) -> None:
        """Wait until ``moment`` on the monotonic clock."""
        time.sleep(max(0.0, moment - time.monotonic()))

    def shows(self, text: str) -> bool:
        """Whether ``text`` is on the screen now."""
        with self.capture.lock:
            return text in self.capture.terminal.text()

    def gone(self, text: str, timeout: float = 120.0) -> None:
        """Wait until ``text`` is not on the screen.

        Raises:
            TimeoutError: The text stayed.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if not self.shows(text):
                return
            time.sleep(0.1)
        raise TimeoutError(f"{text!r} stayed on the screen")

    def highlight(self, label: str, key: str, tries: int = 12) -> None:
        """Press ``key`` until the highlight is on the row of ``label``.

        Raises:
            TimeoutError: The highlight did not come to the row.
        """
        for _ in range(tries):
            if self._highlighted(label):
                return
            self.key(key, pause=0.3)
            self.capture.settle(quiet=0.3, timeout=5.0)
        if not self._highlighted(label):
            raise TimeoutError(f"the highlight did not come to {label!r}")

    def _highlighted(self, label: str) -> bool:
        with self.capture.lock:
            lines = self.capture.terminal.text().splitlines()
        return any("❯" in line and label in line for line in lines)

    @contextlib.contextmanager
    def off_camera(self) -> Iterator[None]:
        """Stop the camera for the body of a ``with`` block, then roll it again."""
        self.camera.cut()
        try:
            yield
        finally:
            self.camera.roll()


def _record(capture: Capture, type_text: Callable[[str], None], path: Path) -> None:
    """Record the tour, and write the recording to ``path``."""
    camera = Recorder(capture)
    _tour(Presenter(capture, type_text, camera))
    camera.cut()
    camera.stop()
    tape = {"grids": camera.grids, "frames": camera.frames, "marks": camera.marks}
    path.write_bytes(pickle.dumps(tape, protocol=pickle.HIGHEST_PROTOCOL))
    print(f"recorded {len(camera.frames) / FPS:.1f} s, {len(camera.grids)} screens", flush=True)


def _tour(p: Presenter) -> None:
    """Type the keys of the tour, and roll the camera on what the video shows."""
    from meshterm.core import preferences
    from meshterm.demo import script

    capture = p.capture
    capture.wait_for("Chat", timeout=60)
    capture.settle(quiet=0.5)

    print("the main menu", flush=True)
    p.camera.roll()
    p.hold(3.0)

    print("the chat, and the paths of a message", flush=True)
    p.type("chat")
    p.hold(0.5)
    p.key("enter")
    p.wait_for("pick a conversation")
    p.hold(1.2)
    p.type("pub")
    p.hold(0.4)
    p.key("enter")
    p.wait_for(screenshots.PATHS_MESSAGE[2])
    p.hold(2.0)
    p.key("up", pause=0.6)
    p.key("up", pause=1.2)
    # A key press can be lost while the transcript draws. The camera does not show the
    # tries: it shows the dialog of the right message as it opens.
    with p.off_camera():
        _open_paths(p)
    p.hold(2.0)
    # The two paths in turn, two times, so that the graph shows which line is which. The
    # video stops on the second path: the first one is often a straight line.
    p.key("down", pause=1.6)
    p.key("up", pause=1.6)
    p.key("down", pause=0.6)
    p.camera.mark("poster")
    p.hold(2.6)
    p.key("escape")
    p.key("escape")  # the first Esc closes the dialog, the second unpicks the message

    print("a message arrives, a question, and its answer", flush=True)
    offset, _, line = next(item for item in script.LIVE_CHANNEL if item[1] == "Public")
    with p.off_camera():
        p.wait_until(_start() + offset - 0.6)
    p.wait_for(line.text, timeout=20)
    p.hold(1.5)
    # The mention is typed in full: the ] makes it a chip, as the video shows.
    p.type("Hey, I wonder if @[Dave-Lachine] is coming Saturday?")
    p.hold(0.8)
    p.key("enter", pause=0.0)
    p.wait_for("0/130")  # the compose line is empty: the message went
    # Dave answers a few seconds later (``meshterm.demo.script.REPLIES``). His answer
    # mentions our node, so it has our star.
    p.wait_for("be there at 2", timeout=30)
    p.hold(3.5)

    print("a trace", flush=True)
    _menu(p, "trace t")
    p.wait_for("pick a target")
    p.hold(1.2)
    p.type("brossard")
    p.hold(0.4)
    p.key("enter")
    p.wait_for("Trace — Brossard")
    p.hold(2.0)
    p.key("enter", pause=0.0)
    p.wait_for("Per-hop medians", timeout=30)
    p.hold(1.0)
    # The history stores walks of one byte for each hop. Thus this walk of two bytes is new,
    # and its records go to the trophy case.
    if p.shows("new record"):
        p.hold(3.0)
        p.key("enter", pause=0.0)  # Close
        p.hold(1.5)
    p.key("pagedown", pause=0.0)
    p.hold(3.0)

    print("the map", flush=True)
    _menu(p, "map", enter=False)
    with p.off_camera():
        p.key("enter", pause=0.0)
        capture.settle(quiet=3.0, timeout=120.0)
    p.hold(3.0)
    for _ in range(3):
        p.key("right", pause=0.4)
    capture.settle(quiet=0.8, timeout=30.0)
    p.hold(1.2)
    p.key(text="l", ctrl=True, pause=0.0)  # back to our node
    capture.settle(quiet=0.8, timeout=30.0)
    p.hold(1.5)
    # Zoom in to the streets around our node, one step at a time, down to zoom 16: the
    # names of the streets and the buildings.
    for _ in range(6):
        p.key("pageup", pause=0.0)
        capture.settle(quiet=0.6, timeout=30.0)
        p.hold(0.9)
    p.hold(3.0)

    print("a repeater administered over the air", flush=True)
    _menu(p, "repeater a")
    p.wait_for("node to manage")
    p.hold(1.2)
    p.type("mont-r")
    p.hold(0.4)
    p.key("enter")
    p.wait_for("Repeater admin — Mont-Royal")
    p.hold(1.5)
    p.key("end", pause=0.6)
    p.highlight("Read settings", "up")
    p.hold(0.8)
    p.key("enter", pause=0.0)
    p.wait_for("Reading — Mont-Royal")
    p.hold(2.0)
    # The read is one round trip for each setting, with the transmit cooldown between two
    # reads: more than three minutes in all. The camera shows the first reads at the real
    # pace. Then, off camera, a short cooldown makes the run shorter, and the video
    # does not show it.
    with p.off_camera():
        cooldown = preferences.current().trace_cooldown_s
        preferences.current().set("trace_cooldown_s", 0.2)
        try:
            p.gone("Reading — Mont-Royal")
        finally:
            preferences.current().set("trace_cooldown_s", cooldown)
        capture.settle(quiet=0.5)
        if p.shows("No reply from"):
            p.key("enter")  # the list of the settings that did not answer
            capture.settle(quiet=0.5)
        p.key("home", pause=0.0)
        capture.settle(quiet=0.5)
    p.hold(3.0)
    p.key("end", pause=0.6)
    p.highlight("Send advert", "up")
    p.hold(0.8)
    p.key("enter", pause=0.0)
    p.wait_for("announce itself")
    p.hold(1.2)
    p.key("enter", pause=0.0)
    p.wait_for("Advert sent", timeout=30)
    p.hold(1.5)
    p.key("enter", pause=0.8)

    print("the live feed, and one packet", flush=True)
    _menu(p, "live f")
    p.wait_for("Live feed")
    p.hold(3.0)
    for _ in range(3):
        p.key("down", pause=0.5)
    p.hold(0.5)
    p.key("enter", pause=0.0)
    p.hold(3.5)
    p.key("escape")
    p.hold(0.5)
    # The end: the main menu, as at the start, with no filter.
    _back_to_menu(p)
    p.hold(2.0)


def _start() -> float:
    """The start of the live traffic of the demo device (refer to :func:`_watch_live_start`)."""
    if _live_start is None:
        raise RuntimeError("the demo device did not start its live traffic")
    return _live_start


def _back_to_menu(p: Presenter) -> None:
    """Go back to the main menu (^W), and clear the filter that it can still hold.

    The Esc waits until the menu is on the screen. If it comes while ^W still unwinds the
    screens, a screen that closes gets it, and the filter stays.
    """
    p.key(text="w", ctrl=True, pause=0.0)
    p.wait_for("What would you like to do?")
    p.hold(0.6)
    p.key("escape")


def _menu(p: Presenter, name: str, *, enter: bool = True) -> None:
    """Go back to the main menu, and type ``name`` to filter it (then Enter)."""
    _back_to_menu(p)
    p.type(name)
    p.hold(0.4)
    if enter:
        p.key("enter")


def _open_paths(p: Presenter) -> None:
    """Open the paths dialog of the message of :data:`screenshots.PATHS_MESSAGE`.

    The message is selected before the call. If the dialog shows another message, close it
    and select the next older message.

    Raises:
        TimeoutError: No dialog showed the message.
    """
    snippet = screenshots.PATHS_MESSAGE[2]
    for _ in range(6):
        p.key(text="p", ctrl=True, pause=0.0)
        p.capture.settle(quiet=1.0)
        quote = p.capture.quote()
        if snippet in quote:
            return
        if quote:
            p.key("escape")
            p.capture.settle(quiet=0.6)
        p.key("up")
        p.capture.settle(quiet=0.6)
    raise TimeoutError(f"no paths dialog showed {snippet!r}")


# --------------------------------------------------------------------------- the video

#: The painter of each worker process (refer to :func:`_paint`).
_painter = None


def _start_painter() -> None:
    global _painter
    _painter = screenshots.TerminalPainter()


def _paint_one(job: tuple[int, Grid, str]) -> str:
    index, grid, folder = job
    path = Path(folder) / f"{index:05d}.png"
    _painter.render(grid).save(path, compress_level=1)
    return str(path)


def _paint(grids: list[Grid], folder: Path) -> list[Path]:
    """Paint each copy of the screen into a PNG file in ``folder``, in parallel.

    Returns:
        The path of the image of each copy, in the order of ``grids``.
    """
    jobs = ((index, grid, str(folder)) for index, grid in enumerate(grids))
    with ProcessPoolExecutor(initializer=_start_painter) as pool:
        return [Path(path) for path in pool.map(_paint_one, jobs, chunksize=8)]


def _encode(frames: list[int], paths: list[Path], work: Path, target: Path) -> None:
    """Encode the video frames into ``target`` with ffmpeg.

    The video frames go to ffmpeg as a list of images, each with the time that it stays on
    the screen (the concat demuxer). Thus each different image goes to ffmpeg one time, and
    the ``fps`` filter makes the video frames again. The RGB to YUV conversion uses the
    BT.709 matrix, which is the matrix that a browser uses for an HD video. The default
    of ffmpeg is the BT.601 matrix, which would change the colours a little.

    The settings come from a comparison on one recording. A keyframe costs approximately
    120 KB (300 KB on the map), and the keyframes were half of the file. Thus there is one
    keyframe each 30 s, and one at each change of scene. CRF 22 looks the same as CRF 20,
    and the file is 3.4 MB instead of 3.8 MB.
    """
    runs: list[list[int]] = []
    for index in frames:
        if runs and runs[-1][0] == index:
            runs[-1][1] += 1
        else:
            runs.append([index, 1])
    lines = ["ffconcat version 1.0"]
    for index, count in runs:
        lines += [f"file '{paths[index].as_posix()}'", f"duration {count / FPS:.6f}"]
    # The concat demuxer uses the duration of the last image only if the image comes again.
    # The image that comes again adds its own time, and -t cuts it.
    lines.append(f"file '{paths[runs[-1][0]].as_posix()}'")
    listing = work / "frames.ffconcat"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    colours = "color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709:range=tv"
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-vf", f"fps={FPS},scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
               f"setparams={colours}",
        "-t", f"{len(frames) / FPS:.3f}",
        "-c:v", "libx264", "-preset", "veryslow", "-tune", "animation", "-crf", "22",
        "-profile:v", "high", "-g", str(FPS * 30),
        "-movflags", "+faststart", "-an", str(target),
    ]  # fmt: skip
    subprocess.run(command, check=True)


def _stills(grid: Grid, seconds: float, folder: Path) -> list[Path]:
    """Write the poster and the card of the video into ``folder``.

    Returns:
        The paths of the two images.
    """
    from PIL import Image

    painter = screenshots.TerminalPainter()
    image = painter.render(grid)
    poster = folder / "demo-poster.png"
    card = folder / "demo-play.png"
    quantize = {"method": Image.Quantize.FASTOCTREE, "dither": Image.Dither.NONE}
    image.quantize(256, **quantize).save(poster, optimize=True)
    _card(image, seconds).quantize(256, **quantize).save(card, optimize=True)
    return [poster, card]


def _card(image, seconds: float):  # noqa: ANN001, ANN202 - an Image, an Image
    """The poster with a play mark and the length of the video.

    The image is a little darker, so that the mark is clear on it. The length is on it,
    because a person who decides to click must know that the video is short.
    """
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont

    width, height = image.size
    ss = 4  # draw at four times the size, then scale down, for smooth edges
    layer = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx, cy = width * ss / 2, height * ss / 2
    radius = width * ss * 0.055
    ring = radius * 0.07
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        fill=(0, 0, 0, 150),
        outline=(*BRAND, 255),
        width=round(ring),
    )
    draw.polygon(
        [
            (cx - radius * 0.3, cy - radius * 0.42),
            (cx + radius * 0.46, cy),
            (cx - radius * 0.3, cy + radius * 0.42),
        ],
        fill=(*BRAND, 255),
    )
    minutes, rest = divmod(round(seconds), 60)
    label = f"{minutes}:{rest:02d}"
    font = ImageFont.truetype(screenshots.TEXT_FONTS[0], round(width * ss * 0.024))
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
    pad = font.size * 0.45
    margin = width * ss * 0.012
    box = [
        width * ss - margin - (right - left) - 2 * pad,
        height * ss - margin - (bottom - top) - 2 * pad,
        width * ss - margin,
        height * ss - margin,
    ]
    draw.rounded_rectangle(box, radius=font.size * 0.3, fill=(12, 12, 12, 225))
    draw.text((box[0] + pad - left, box[1] + pad - top), label, font=font, fill=(*BRAND_LIGHT, 255))
    layer = layer.resize((width, height), Image.Resampling.LANCZOS)
    base = ImageEnhance.Brightness(image.convert("RGB")).enhance(0.75).convert("RGBA")
    return Image.alpha_composite(base, layer).convert("RGB")


# --------------------------------------------------------------------------- the README


def _publish_readme(video: Path) -> str:
    """Upload ``video`` to GitHub for the README, and put its URL in ``README.md``.

    GitHub plays a video in a README only when the video is one of its own attachments.
    In the web page, a file becomes an attachment when a person drops it into a comment.
    This function does the same upload through an endpoint that GitHub does not document:
    a POST of the file to ``uploads.github.com/user-attachments/assets``, with the token of
    the GitHub CLI (https://island94.org/2026/08/programmatically-upload-attachments-to-github-issues-pull-requests-comments).
    The attachment belongs to the repository, not to an issue or a comment. Thus no issue
    is necessary, and nothing is left to delete. GitHub shows it to each visitor of the
    README, also one who is not logged in. The previous attachment stays on GitHub, but
    nothing links to it. GitHub has no way to delete an attachment.

    The function replaces the one line of the README that holds the URL of an attachment
    (:data:`ATTACHMENT_LINE`), and nothing else.

    Returns:
        The URL of the new attachment.

    Raises:
        SystemExit: The video, the GitHub CLI, or the line of the README is missing, or
            GitHub refused the upload.
    """
    if not video.is_file():
        raise SystemExit(f"video.py: {video} does not exist. Make the video first.")
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    if len(ATTACHMENT_LINE.findall(text)) != 1:
        raise SystemExit("video.py: README.md must have exactly one line with an attachment URL")
    if shutil.which("gh") is None:
        raise SystemExit("video.py: the GitHub CLI (gh) is not on the PATH")

    def gh(*args: str) -> str:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
        if done.returncode:
            raise SystemExit(f"video.py: gh {args[0]} failed: {done.stderr.strip()}")
        return done.stdout.strip()

    token = gh("auth", "token")
    query = urllib.parse.urlencode(
        {
            "name": video.name,
            "content_type": "video/mp4",
            "repository_id": gh("api", f"repos/{REPOSITORY}", "--jq", ".id"),
        }
    )
    request = urllib.request.Request(
        f"https://uploads.github.com/user-attachments/assets?{query}",
        data=video.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "video/mp4",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as reply:
            url = json.load(reply)["url"]
    except (OSError, KeyError, ValueError) as exc:
        raise SystemExit(f"video.py: GitHub refused the upload: {exc}") from None
    if not ATTACHMENT_LINE.fullmatch(url):
        raise SystemExit(f"video.py: GitHub returned an unexpected URL: {url}")
    readme.write_bytes(ATTACHMENT_LINE.sub(url, text).encode("utf-8"))
    return url


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
