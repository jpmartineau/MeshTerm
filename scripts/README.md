# scripts/

These are tools for the repository. None of them is in the wheel.

| Script | What it is for |
|---|---|
| `basemap-doctor.py` | Finds why a user's map has no basemap: certificates, DNS, proxy, or colour depth |
| `cardputer-zero/` | `build-deb.sh` builds the Debian package for M5Stack's app store. Run it on the Cardputer Zero or on another arm64 Debian 13 system. `store/` holds the listing for the store (`app-builder.json`, which `czdev publish` reads), its icon, and its screenshots. `screenshot.py` saves the display of a Cardputer as a 320×170 PNG over SSH, as the store wants a screenshot. `store-publish.py` sends a package to the store with M5Stack's `czdev`, with two corrections. The release workflow runs it after each release. `launcher-entry.sh` puts a source checkout in the Cardputer Zero's app launcher. `remove` takes it out. `meshterm.png` is the package's icon: the website's logo, at the launcher's 100×100 size. `meshterm-dev.png` is its negative, for the checkout's entry, so that the two entries look different. `type-text.py` types a pasted string (an API key, a Wi-Fi password) on the Cardputer's keyboard from another computer, over SSH |
| `screenshots.py` | Makes the screenshots of the website from the demo mesh (`--demo`): the main menu, the chat, the paths of a message, and the map, for the terminal, the Cardputer Zero, and the PicoCalc. They are the same at each run. The result goes to `docs/screenshots/<set>/`. It also draws the two wordmarks of the splash into `docs/screenshots/splash/`, which the website uses as its splash |
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
