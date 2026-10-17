# scripts/

These are tools for the repository. None of them is in the wheel.

| Script | What it is for |
|---|---|
| `basemap-doctor.py` | Finds why a user's map has no basemap: certificates, DNS, proxy, or colour depth |
| `cardputer-zero/` | `build-deb.sh` builds the Debian package for M5Stack's app store. Run it on the Cardputer Zero or on another arm64 Debian 13 system. `store/` holds the listing for the store (`app-builder.json`, which `czdev publish` reads), its icon, and its screenshots. `screenshot.py` saves the display of a Cardputer as a 320×170 PNG over SSH, as the store wants a screenshot. `store-publish.py` sends a package to the store with M5Stack's `czdev`, with two corrections. The release workflow runs it after each release. `launcher-entry.sh` puts a source checkout in the Cardputer Zero's app launcher. `remove` takes it out. `meshterm.png` is the package's icon: the website's logo, at the launcher's 100×100 size. `meshterm-dev.png` is its negative, for the checkout's entry, so that the two entries look different. `type-text.py` types a pasted string (an API key, a Wi-Fi password) on the Cardputer's keyboard from another computer, over SSH |
| `screenshots.py` | Makes the screenshots of the website from the demo mesh (`--demo`): the main menu, the chat, the paths of a message, and the map, for the terminal, the Cardputer Zero, and the PicoCalc. They are the same at each run. The result goes to `docs/screenshots/<set>/`. It also draws the two wordmarks of the splash into `docs/screenshots/splash/`, which the website uses as its splash |
| `video.py` | Makes the demo video of the website from the demo mesh: a tour of less than two minutes in the terminal, with its poster and its play card. The result goes to `src/` of the `meshterm.net` checkout beside this one |
| `reels.py` | Makes the videos for Instagram from the demo mesh: one vertical video for each page, with subtitles in the picture and no sound, a cover, and a caption for the post. The tours are in `reel_tours.py`. The result goes to `output/reels/` |
| `picocalc-lyra/` | Setup of the Calculinux device: console font, palette, Wi-Fi. `xiao-radio/` is the kit for the XIAO radio (firmware build, flashing, UART setup) |
| `uconsole/` | The uConsole's always-on SPI bridge service. MeshTerm can also control that radio directly, with no script (refer to `docs/devices/uconsole.md`) |

## basemap-doctor.py

The map is the only part of MeshTerm that uses the network. MeshTerm logs each fetch
failure on this path at DEBUG, and `log_level` is WARNING by default. Thus a user whose
tiles do not arrive sees a blank ground and no explanation. This script prints the
explanation and the correction.

To use it, tell the user to run this **on the machine that has the problem**:

```bash
curl -fsSL https://raw.githubusercontent.com/jpmartineau/MeshTerm/main/scripts/basemap-doctor.py -o /tmp/basemap-doctor.py
python3 /tmp/basemap-doctor.py
```

Any `python3` is correct. If that `python3` does not have MeshTerm, the script reads the
interpreter from the installed `meshterm` command's shebang, and it runs again with
that interpreter. Thus the answer always comes from **the app's own Python**. A Mac
often has three Pythons, and only one of them has the trust store that is important. Save
the script to a file, as in the example. Do not pipe it into `python3 -`. When there is no
file on the disk, the script cannot run again with the other interpreter.

Ask the user to paste all the output back to you. The output names the interpreter, the
certificate store, the tile source, the number of cached tiles, and `COLORTERM`. Then it
gives one verdict:

- **curl reached it, Python did not.** The Python's certificate store is empty. A
  python.org install on macOS has an empty store until the user runs
  `Install Certificates.command`.
- **neither reached it.** The problem is in the network path: a firewall, a proxy, a DNS
  filter, or an outage. There is nothing to correct in MeshTerm.
- **both reached it.** The tiles are correct. The problem is colour. When `COLORTERM` is not
  set, the terminal uses 256 colours, and the greens and blues of the basemap are too
  similar. Terminal.app cannot do 24-bit colour.

The exit status is 0 when the tiles arrive and 1 when they do not. Thus a script can use it.

## screenshots.py

The website shows the same four screens for three platforms: the terminal, the Cardputer
Zero, and the PicoCalc. This script makes all twelve images from the demo mesh. Each run
gives the same pixels: the demo writes the same history, the clock of each run is set to
11:42 on Thursday 15 October 2026 in Montréal, and nothing moves on the screen when the
script saves it.

```bash
pip install -e .[dev]           # Pillow, fontTools, and time-machine
meshterm emulate --fetch-fonts  # the Terminus font of the handheld sets, one time
python scripts/screenshots.py   # all three sets, into docs/screenshots/<set>/
python scripts/screenshots.py --only terminal
```

No window opens. MeshTerm runs in the process, in the session of the emulator, and the
script types the keys. The terminal set uses Cascadia Mono, Segoe UI Symbol, and Segoe UI
Emoji, which Windows has. On another system, change `TEXT_FONTS` and `EMOJI_FONT` at the
top of the script. The first run downloads the tiles of the map. The later runs use the
tile cache in the temporary directory.

The script also draws each wordmark of the startup splash (`meshterm/assets/splash/*.ans`)
as `docs/screenshots/splash/logo.<width>.png`, one image pixel for each pixel of the font,
in the Campbell colours. The website takes its splash from there: the wide mark on a wide
screen, and the narrow mark on a phone. `--only splash` draws only these two images.

After a change to a screen or to the art, run the script again, then rebuild the website
(refer to the README of the `meshterm.net` repository).

## video.py

The website plays a video of MeshTerm under its buttons. This script makes it from the
demo mesh, in the terminal set of `screenshots.py`: the same 80×24 terminal, the same
clock, and the same painter. It types the keys of a tour of less than two minutes: the
chat, the paths of a message, a question that mentions a node and its answer, a trace,
the map, Repeater admin, and the live feed. A camera copies the screen 30 times each second.

```bash
python scripts/video.py                             # into ../meshterm.net/src/
python scripts/video.py --out some/folder
python scripts/video.py --recording tour.pickle     # keep the recording
python scripts/video.py --readme                    # the README's copy, after a check
```

It needs ffmpeg with libx264 on the `PATH`, and the needs of `screenshots.py`. A run takes
approximately four minutes. The tour runs on the real clock, then the script paints each
different screen in parallel and encodes the video. With `--recording`, the script keeps
the recording in that file. When the file exists, a later run paints and encodes it again,
and does not run MeshTerm.

The script writes three files: `demo.mp4`, `demo-poster.png` (the paths of a message,
which the website shows before the video plays), and `demo-play.png` (the poster with a
play mark and the length of the video). The camera stops while the tour waits for
something slow: the tiles of the map, and most of the read of the repeater's settings.
Thus the video jumps over the wait.

With `--readme`, the script makes no video. It uploads `demo.mp4` to GitHub as an
attachment of the repository, because GitHub plays a video in a README only when it is one
of its own attachments. Then it puts the new URL on the attachment line of `README.md`. It
uses an endpoint that GitHub does not document, with the token of the GitHub CLI. Each
upload stays on GitHub, so use it only on a video that you examined.

Each run gives the same video to the eye, but not the same file, because the camera copies
the screen on the real clock. Each release makes the video again, after the screenshots,
and pushes it to the website repository before the tag. After a change to the screens
between two releases, run the two scripts, then rebuild the website (refer to the README of
the `meshterm.net` repository).

## reels.py

This script makes the videos for Instagram: one for each page of MeshTerm, in the demo mesh.
The written pages (About MeshTerm, About the author, Join Discord, and Support MeshTerm)
have no video. Each video is a Reel: a vertical video of 1080×1920 pixels, at 30 video
frames each second. The terminal has 72 columns, so that its text is as large as the width
of a phone lets it be. Above the terminal is the name of the series
(`MeshTerm for MeshCore · Chat`), and under it are the subtitles. The video has no sound,
because many people watch Instagram with the sound off. It ends on a card with the wordmark
and the address of the website.

```bash
python scripts/reels.py all                             # each video, into output/reels/
python scripts/reels.py chat map                        # only these two
python scripts/reels.py chat --out some/folder
python scripts/reels.py all --recordings some/folder    # keep the recordings
```

The tours are in `reel_tours.py`, with the subtitles and the caption of each video, in the
`REELS` table. To add a video, write its tour and add it to `REELS`. A tour types the keys,
as in `video.py`, and also writes the subtitles (`Director.subtitle`). It opens its page
off camera, so that the video starts on the page.

For each video, the script writes three files: `<name>.mp4` (the video, with a silent audio
track), `<name>-cover.png` (the cover of the Reel), and `<name>.txt` (a caption for the
post). It needs the same things as `video.py`, but not the website checkout. One video
takes approximately two minutes, and all of them approximately 45 minutes. With
`--recordings`, the script keeps each recording in that folder, and a later run paints and
encodes it again without MeshTerm.
