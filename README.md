# MeshTerm

**A full-featured TUI [MeshCore](https://meshcore.io/) client for your terminal.** Use it
to tune your radio, chat across the mesh, and watch the network live. It also keeps a
long-term record of everything that you overhear. It works with a companion that is
connected by serial, Bluetooth, or network (TCP).

MeshTerm is interactive by default. It is a modern TUI that you operate with the keyboard,
built on [Rich](https://github.com/Textualize/rich) and
[prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit). You can also
use it in scripts. **Most screens have a mirror `meshterm` subcommand.** Thus the same
functions work for an evening of exploration on the mesh and for a cron job. MeshTerm
stores each packet that the radio overhears in a local SQLite database. The longer you
run MeshTerm, the more the history of your mesh is worth.

<p>
  <a href="https://github.com/jpmartineau/MeshTerm/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/jpmartineau/MeshTerm/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-green"></a>
  <img alt="For MeshCore" src="https://img.shields.io/badge/for-MeshCore-8A2BE2">
  <img alt="Interface: TUI + CLI" src="https://img.shields.io/badge/interface-TUI%20%2B%20CLI-orange">
  <a href="https://github.com/jpmartineau/MeshTerm/releases/latest"><img alt="Download" src="https://img.shields.io/badge/download-latest-brightgreen"></a>
  <a href="https://discord.gg/AZwe5Uvb3S"><img alt="Discord" src="https://img.shields.io/badge/chat-Discord-5865F2"></a>
</p>

> MeshTerm is a side project, and one person runs it. We welcome bug reports. If you want to
> change something, first say hello on [Discord](https://discord.gg/AZwe5Uvb3S).
> [Read more about how the project is run](#how-this-project-is-run).

https://github.com/user-attachments/assets/f91a6695-14d5-4ae0-8fdc-e65492d0955d

<p align="center"><sub>A video of less than two minutes, made on a real mesh.</sub></p>

<p align="center">
  <img src="docs/screenshots/dashboard.png" alt="The dashboard: a timeline of the packets for each minute over the last two hours, and a count for each class of packet" width="49%">
  <img src="docs/screenshots/map.png" alt="The map: repeaters with their names, on an OpenStreetMap basemap of the Lachine Canal" width="49%">
</p>
<p align="center">
  <img src="docs/screenshots/trace.png" alt="A trace: the route that the trace went through, drawn as chips, then the SNR of each hop" width="49%">
  <img src="docs/screenshots/path.png" alt="Message paths: each route over which a channel message was heard, drawn on the map" width="49%">
</p>
<p align="center">
  <img src="docs/screenshots/picocalc.png" alt="MeshTerm on the PicoCalc, a console that is 53 columns wide, with a radio installed inside it" width="38%">
</p>
<p align="center"><sub>The dashboard, the map, a trace, the paths of a message, and the whole app on a PicoCalc.</sub></p>

---

## Why MeshTerm

- **One tool, two faces.** To open a full-screen menu, start `meshterm`. To run one scripted
  task, give a subcommand. The same registry builds the menu and the CLI, so they do not
  become different from one another.
- **It remembers.** A passive monitor stores each advert and each telemetry packet that it
  overhears. This includes the SNR, the RSSI, and the shared position. The monitor starts
  when the radio opens. The maps, the contact lists, the charts, and the Time Machine all
  read this history.
- **It measures.** It has a live trace with the reliability of each hop. It has a TX-power
  sweep that goes from coarse to fine and then verifies the result. It also has a graph
  that you can walk through, which shows how the mesh is really connected.
- **It talks.** You can send live messages on a channel and direct messages. You can use the
  message boards of room servers. A courier stores messages and sends them when a contact
  is not yet there. You can share a channel as a QR code or as a `meshcore://` link.
- **It connects in the same way as your companion.** It supports serial (USB), Bluetooth
  LE, and TCP (a Wi-Fi board or a network proxy). It finds the serial and Bluetooth
  devices automatically, and you select one from a list. You type the name of a network
  device. MeshTerm reconnects live when a link of any kind drops. On Windows and Linux,
  MeshTerm pairs with a Bluetooth companion that has a PIN. When a connection fails,
  MeshTerm says which step failed and what to do. If you have no radio, a built-in
  simulator is available for development.
- **It works offline.** The street map caches OpenStreetMap tiles on disk. When there is no
  network, it shows a blank grid. MeshTerm never needs the internet to run.

## Install

### Download a build

A build is one file. You do not need Python. Builds are available for Windows, macOS (Intel
and Apple silicon), and Linux (x64 and ARM64, so the uConsole is included). Each command
below gets the
**[⬇ latest release](https://github.com/jpmartineau/MeshTerm/releases/latest)**, whichever
release that is. Thus the commands do not become old.

MeshTerm is a terminal program, so **open a terminal and run it from there.** You can
double-click the file and it will work. But then your system selects the console, and you
cannot choose it. If an error occurs at startup, the window closes before you can read the
error.

#### macOS: download it with `curl`

**Do not use your browser.** A browser marks the file as downloaded, and then macOS does not
let you open it. On Sequoia and later, right-click → Open does not help. `curl` does not
mark the file, so the file runs. Open Terminal and paste:

```bash
curl -fL -o meshterm https://github.com/jpmartineau/MeshTerm/releases/latest/download/meshterm-macos-arm64
chmod +x meshterm
./meshterm
```

This is the build for Apple silicon Macs (M1 and later). On an **Intel** Mac, replace
`arm64` with `x64`. If you already downloaded the file with a browser, you do not have to
start again. Run `xattr -d com.apple.quarantine meshterm` to clear the mark.

#### Linux: the same three lines

Use `linux-x64` on a PC. Use `linux-arm64` on a uConsole or another 64-bit ARM handheld.

```bash
curl -fL -o meshterm https://github.com/jpmartineau/MeshTerm/releases/latest/download/meshterm-linux-x64
chmod +x meshterm
./meshterm
```

#### Windows

Download
**[meshterm-windows-x64.exe](https://github.com/jpmartineau/MeshTerm/releases/latest/download/meshterm-windows-x64.exe)**.
Then open Windows Terminal or PowerShell, use `cd` to go to your downloads folder, and run
the file:

```powershell
.\meshterm-windows-x64.exe
```

The first time, Windows says *"Windows protected your PC"*. Click **More info → Run anyway**.

After these steps, you have one file: `meshterm` on macOS and Linux, or
`meshterm-windows-x64.exe` on Windows. Put the file in a folder that is in your `PATH`. On
Windows, rename it to `meshterm.exe`. Then you can run `meshterm` from any folder.

> **These builds are not code-signed yet.** For this reason, macOS and Windows both show a
> warning the first time. **Windows signing will come.** MeshTerm applied to the
> [SignPath Foundation](https://signpath.org), which signs open-source projects for free.
> After the setup is complete, each Windows download will be signed. The
> [code signing policy](#code-signing-policy) below gives the details. The Mac builds will
> stay unsigned, because Apple charges a fee for signing and this is a free side project.
> Each release has a `SHA256SUMS` file. Use it to check the file that you got.
> [Installing with pip or pipx](#or-install-with-pip) avoids the problem, because you do not
> download a binary.
>
> The page of each release also has these commands, written for **that exact version**, next
> to its own downloads.

### If your terminal draws empty boxes

MeshTerm draws emoji icons, braille charts, and powerline path chips. Your *terminal*
decides if you see them, not your font. Windows Terminal, the terminal of VS Code, and each
modern terminal on macOS and Linux draw them correctly.

The classic Windows console does not draw them. This is the black `cmd.exe` window that
opens when you double-click the file. When MeshTerm starts there, it **moves to Windows
Terminal**, tells you, and opens there. It does not install anything and does not change
anything. If Windows Terminal is not available, MeshTerm offers the charts and marks without
the icons. It also installs Microsoft's [Cascadia Mono PL](https://github.com/microsoft/cascadia-code)
for you. This needs no administrator rights, and it downloads nothing.

To stay in the same console, turn off these actions in Preferences → Display → Console
setup. [Terminals, icons, and the Windows console](docs/guide/terminals.md) explains why
they are necessary.

> **Try a build without a change to your real data.** MeshTerm keeps all its data in
> `~/.meshterm`. This includes your history database, your contacts, and your channel
> keys. *Each* copy of MeshTerm uses this same folder. To try a copy in isolation, set
> `MESHTERM_HOME`:
>
> ```bash
> MESHTERM_HOME=~/meshterm-test ./meshterm              # macOS, Linux
> ```
> ```powershell
> $env:MESHTERM_HOME = "$HOME\meshterm-test"; .\meshterm-windows-x64.exe
> ```
>
> Use the same variable if you run two radios and you want to keep them separate.

### Or install with pip

You need **Python 3.10 or newer**. This is the only requirement. MeshTerm installs all
other parts by itself.

```bash
pip install git+https://github.com/jpmartineau/MeshTerm
meshterm
```

`meshterm` opens the full-screen menu. This is the complete installation.

**Do you not want to change your system Python?** [pipx](https://pipx.pypa.io/) puts the
app in its own private environment. It also gives you a normal `meshterm` command:

```bash
pipx install git+https://github.com/jpmartineau/MeshTerm
```

**Do you not have a radio yet?** `meshterm --mock` runs the complete app on a simulated
mesh. You can look at the app before you buy anything.

### Coming

**`pip install mesh-term`** will work after the package is published. It is not published
yet, because the question of the name is not resolved
([read why](https://github.com/pypi/support/issues/12162)).

To set up a development environment, refer to [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

**[The documentation index](docs/README.md)** lists all the documents. This includes the
code survey, which we keep as a historical record. This table is a short version:

| Read | For |
| --- | --- |
| **[The command line](docs/cli/README.md)** | Each subcommand and option, what each prints on the plain face and on the JSON face, the exit statuses, and recipes. |
| **[What MeshTerm does](docs/guide/features.md)** | Each screen that the menu has, and the command that does the same job without the menu. |
| [The CLI cookbook](docs/cli/cookbook.md) | Common one-line commands, in groups by the task that you want to do. |
| [Configuring MeshTerm](docs/guide/configuration.md) | The preferences, the device profiles, and all the files that MeshTerm keeps in `~/.meshterm`. |
| [When a companion does not connect](docs/guide/connecting.md) | Bluetooth pairing on each system, how to find the PIN, USB permissions, and each connection error, with the action to take. |
| [MeshTerm on hardware](docs/devices/README.md) | Which handheld manual is the correct one for you: the [PicoCalc](docs/devices/picocalc-lyra.md) build, or the [uConsole](docs/devices/uconsole.md), whose LoRa chip MeshTerm can control directly. The [Cardputer Zero](docs/devices/cardputer-zero.md) is **not supported yet**. |
| [How the code is laid out](docs/development/architecture.md) | The layers of the code, and where to put a new feature. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | The development setup and the house rules. |
| [CHANGELOG.md](CHANGELOG.md) | What changed, with the newest change first. |

## Quick start

```bash
# Interactive full-screen menu (auto-discovers serial + Bluetooth companions)
meshterm

# Connect over Bluetooth (address from `meshterm devices`); add --ble-pin if it has a PIN
meshterm --ble AA:BB:CC:DD:EE:FF --ble-pin 123456 info

# Connect over the network to a TCP companion (host[:port], port defaults to 5000)
meshterm --tcp 192.168.1.50 info

# Drive a LoRa radio on this machine's own SPI bus (the uConsole AIO; Linux only)
meshterm --spi info

# No radio attached? Use the built-in simulator for development.
meshterm --mock

# On a ClockworkPi PicoCalc (Luckfox Lyra / Calculinux) the handheld flavour is
# auto-detected: a 53-column layout, a 16-slot palette, a compact glyph language on a
# custom console font, and an F-key hint lane. Preview it anywhere, or inspect it:
meshterm --mock --platform picocalc-lyra
meshterm specimen
```

MeshTerm has its own manuals for handhelds. **[MeshTerm on the
PicoCalc](docs/devices/picocalc-lyra.md)** goes from the shopping list for a stock PicoCalc
to a Linux handheld that has a LoRa radio soldered inside. **[MeshTerm on the
uConsole](docs/devices/uconsole.md)** shows how to control the SPI LoRa board of a uConsole
directly with `--spi`. It also shows how to use a small bridge, if you want the node to be
on the mesh at all times. M5Stack's Cardputer Zero also has a page, but it is **[not
supported yet](docs/devices/cardputer-zero.md)**. The scripts that the manuals run on the
device are in [`scripts/`](scripts/). If you do not know which manual is correct for you,
refer to [`docs/devices/README.md`](docs/devices/README.md).

Does a companion not connect? **[When a companion does not
connect](docs/guide/connecting.md)** explains Bluetooth pairing on Windows, Linux, and
macOS. It also explains the `dialout` group and ModemManager for USB on Linux, and each
error message that MeshTerm gives.

## What it does

The menu asks one question: *what would you like to do?* It gives the answer in two halves.
Three sections name an activity. **Message** has chat, channels, the courier outbox, and
contacts. **Watch** has the dashboard, the live feed, the watchtower, and the time machine.
**Explore** has the map, the mesh walk, tracing, and the trophy case. The other three
sections name the owner. **This node** is the radio in your hand. **Other nodes** is the
someone else's radio, which you reach over the mesh. **This app** is MeshTerm itself, with
the preferences, the diagnostics, and the written pages.

Almost all of these have a mirror `meshterm` subcommand. The same registry builds the menu
and the CLI, so they do not become different from one another. The live screens, such as the
map and the dashboard, are only in the menu.

**[What MeshTerm does](docs/guide/features.md)** is the complete catalogue. It shows each
screen, what the screen does, and the scripted equivalent where there is one.

## Scripting it

Almost every command accepts `--json`. The global options are `--profile/-p`, `--port`,
`--ble`, `--ble-pin`, `--tcp`, `--spi`, `--mock`, `--db`, `--json`, `--absolute`,
`--quiet/-q`, and `--platform`. You can type them before or after the subcommand.

```bash
# Every repeater's public key, for a script
meshterm contacts --json | jq -r '.[] | select(.node.type == "repeater") | .node.key'

# A trace transmits exactly once; run it again to sample more
meshterm trace --target Alice --profile yagi

# Messaging, and store-and-forward for a contact who isn't there yet
meshterm chat send --channel 0 "net in 5"
meshterm courier queue Alice "ping me when you're back" --at 18:30

# The radio's own settings, and a passive capture window
meshterm config set radio_sf 9
meshterm monitor --seconds 60
```

**[The command line](docs/cli/README.md)** is the manual. It shows each command and option,
what each prints on both faces, and the exit statuses.
**[The CLI cookbook](docs/cli/cookbook.md)** has more one-line commands like these.

## How this project is run

One person works on MeshTerm, in the evenings and at weekends. I want to tell you this at
the start, so that you do not have to guess from the time that things take.

**We welcome all issues.** This includes bugs, questions, and "is this a fault or is it
correct". The
[bug form](https://github.com/jpmartineau/MeshTerm/issues/new?template=bug.yml) asks for
much information. This is intentional. The quality of the description of a problem
decides if I can do something about it. If I can reproduce a problem, I will usually work
on it. If I cannot reproduce it, I can only guess.

**Ask before you write a pull request.** Join the [Discord](https://discord.gg/AZwe5Uvb3S)
and say hello in `#contributing`. Tell us what you want to change. Then wait for a yes.
This applies to small corrections also. This is not because I am too careful. MeshTerm has
firm house rules for how to build screens (they are in [CLAUDE.md](CLAUDE.md)). I do not
want you to spend a weekend on a change that I then ask you to write again. A short
conversation at the start saves time for both of us. You can use AI tools, but you must say
that you used them. [CONTRIBUTING.md](CONTRIBUTING.md) has the details.

**I cannot promise a time.** Some problems are corrected the same night. Some wait for a
month, because I had other tasks in my life. If your issue is quiet for a long time, please
remind me. This helps me.

**Where to report a problem.** You can use one of these two places:

- **[GitHub issues](https://github.com/jpmartineau/MeshTerm/issues)**, if you have an
  account. We track and correct problems here, so this is the shortest path.
- **[Discord](https://discord.gg/AZwe5Uvb3S)**, if you do not have an account, or if you
  are not sure that the problem is a bug. Ask in the support forum, or put defects that you
  can reproduce in `#bugs`. I move the real bugs to GitHub myself.

Do not worry about the choice of the wrong place. It is better that you tell us about a
problem than that you file it in a tidy way.

More documents: [CONTRIBUTING.md](CONTRIBUTING.md) ·
[SECURITY.md](SECURITY.md) ·
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) ·
[CHANGELOG.md](CHANGELOG.md)

## Code signing policy

Free code signing provided by [SignPath.io](https://signpath.io), certificate by
[SignPath Foundation](https://signpath.org).

This policy is for the Windows download. The setup is in progress, so the releases up to
and including 0.10.2 are still unsigned.

**How a build is signed.** GitHub Actions makes each Windows build, directly from the public
source code in this repository. SignPath signs only a file that it can trace to one of
these builds, and only after the maintainer approves that release. This process cannot sign
a file that was built on any person's own computer, and this includes the maintainer's
computer.

**Who does what.** MeshTerm has one maintainer,
[Jean-Pierre Martineau](https://github.com/jpmartineau), who has all three roles:

- **Author:** writes the code and commits it.
- **Reviewer:** reviews each change from anyone else before it goes in.
- **Approver:** approves each release before it is signed.

**Privacy.** This program will not transfer any information to other networked systems
unless specifically requested by the user or the person installing or operating it.

In practice, MeshTerm goes online for only one purpose: map tiles. It downloads them from
[OpenFreeMap](https://openfreemap.org/) when you open a screen that shows a map. The
[privacy policy](https://openfreemap.org/privacy) of OpenFreeMap applies to these
downloads. MeshTerm collects no information about you, and it does not check for updates.
Over the radio, MeshTerm sends only what you tell it to send.

<!-- ste: off -->
<!-- Legal text: attribution and licence. It is not in ASD-STE100, on purpose. -->

## Acknowledgements & attribution

MeshTerm stands on the [MeshCore](https://meshcore.io/) project — its firmware and the
`meshcore` Python companion library are what make talking to the radio possible.

### Map data

The street map is rendered from **© OpenStreetMap contributors** data, served as vector
tiles by [OpenFreeMap](https://openfreemap.org/) on the unmodified
**© OpenMapTiles** [schema](https://openmaptiles.org/). OpenStreetMap data is available
under the [Open Database License (ODbL)](https://www.openstreetmap.org/copyright). Tiles
are decoded with a small built-in reader for the
[Mapbox Vector Tile](https://github.com/mapbox/vector-tile-spec) format.

Both map surfaces carry the credit themselves. The full-screen map opens with
`© OpenMapTiles · Data from OpenStreetMap` in its bottom-right corner and collapses it to
`© OpenStreetMap` once you pan, zoom, or type — set into the panel's bottom border rule on
a desktop terminal, so the drawing itself keeps every cell, and on the map's own last row
on the PicoCalc, whose frame has no bottom rule. The node page's location preview shows the
short form throughout. The About page inside the app spells out the licence URL.

### Bundled font

MeshTerm ships **Cascadia Mono PL**, © 2019–present Microsoft Corporation, redistributed
unmodified under the [SIL Open Font License 1.1](meshterm/assets/fonts/CascadiaMono-OFL.txt).
It's offered to Windows users whose console can't draw the charts — see
[Terminals, icons, and the Windows console](docs/guide/terminals.md). Microsoft doesn't endorse MeshTerm; the font is
simply the right tool, being one of the very few monospace faces that carries the braille
block the timelines are drawn from.

### Open-source dependencies

MeshTerm is built with these libraries; each is used under its own license (see the
respective project for the authoritative terms):

| Library | Role | License |
| --- | --- | --- |
| [meshcore](https://pypi.org/project/meshcore/) | Companion-device protocol (serial / BLE / TCP) | MIT |
| [pycryptodome](https://www.pycryptodome.org/) | AES/HMAC for decrypting overheard channel packets | BSD-2-Clause / Public Domain |
| [pyserial](https://github.com/pyserial/pyserial) | Serial-port enumeration and I/O | BSD-3-Clause |
| [bleak](https://github.com/hbldh/bleak) | Bluetooth LE scanning + connection | MIT |
| [Typer](https://typer.tiangolo.com/) | Scripted CLI (subcommands mirror the menu) | MIT |
| [Rich](https://github.com/Textualize/rich) | Console rendering | MIT |
| [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) | Full-screen interactive TUI | BSD-3-Clause |
| [markdown-it-py](https://github.com/executablebooks/markdown-it-py) | CommonMark parser behind the written About pages | MIT |
| [Segno](https://github.com/heuer/segno) | Pure-Python QR codes (channel share links) | BSD-3-Clause |
| [tomli](https://github.com/hukkin/tomli) / [tomli-w](https://github.com/hukkin/tomli-w) | TOML config, preferences, and device-config backups | MIT |

## License

Copyright © 2026 Jean-Pierre Martineau.

MeshTerm is open source under the [Apache License, Version 2.0](LICENSE) — free to
use, modify, and redistribute, commercially or otherwise. The "MeshTerm" name and any
associated logo are trademarks reserved by the author and aren't covered by the code
license (see [NOTICE](NOTICE)); a redistributed fork should go by its own name. The
donate link built into the app supports this project and its original author —
forks that keep soliciting through it without redirecting it to themselves aren't
affiliated with this project.

The bundled font is **not** covered by that license. `meshterm/assets/fonts/CascadiaMonoPL.ttf`
is Microsoft's Cascadia Mono PL, redistributed unmodified under the SIL Open Font License
1.1, which travels with it as
[CascadiaMono-OFL.txt](meshterm/assets/fonts/CascadiaMono-OFL.txt) and stays its only
license.

Each standalone build is a single file with `LICENSE`, `NOTICE`, and a generated
`THIRD-PARTY-NOTICES.txt` (every dependency's own license text) bundled inside it. The same
three files are also attached to each release on the
[releases page](https://github.com/jpmartineau/MeshTerm/releases), so you can read them
without running anything.

<!-- ste: on -->
