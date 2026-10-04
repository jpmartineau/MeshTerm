# MeshTerm on hardware

MeshTerm talks to a **node** — a MeshCore radio that speaks the companion protocol over
serial, Bluetooth LE, or TCP. Plug a companion board into USB, or pair one over Bluetooth,
and none of this page applies: MeshTerm finds it, and the [README](../../README.md) and the
[command line manual](../cli/README.md) are all you need.

This page is for the hardware that *doesn't* present a node that way. Each such device has
its own step-by-step manual, and this page only tells you which one is yours.

## Which guide is yours

| I have… | Read |
| --- | --- |
| a ClockworkPi **PicoCalc**, stock or already running Calculinux on a Luckfox Lyra, and I want MeshTerm on its own screen | **[MeshTerm on the PicoCalc](picocalc-lyra.md)** — from the shopping list to a working machine |
| …and I want a **radio** inside it | the same manual, [Phase 3](picocalc-lyra.md#phase-3--add-the-radio) — a XIAO nRF52840 + Wio-SX1262 soldered to the Lyra's UART |
| a ClockworkPi **uConsole** with the hackergadgets AIO board — an SX1262 on the host's own SPI bus | **[MeshTerm on the uConsole](uconsole.md)** — MeshTerm drives it directly with `--spi`, or a bridge service fronts it as a companion if you want it always on |
| an M5Stack **Cardputer Zero** — the one with a Raspberry Pi inside, not the ESP32 Cardputer or Cardputer-Adv | **[MeshTerm on the Cardputer Zero](cardputer-zero.md)** — ⚠️ **not supported yet**: it has never run on the device; the page says what works in the emulator |
| a MeshCore companion on USB, Bluetooth, or Wi-Fi | nothing here — the [README](../../README.md), and [When a companion won't connect](../guide/connecting.md) if it doesn't |

The devices have nothing in common but MeshTerm itself. The device-side scripts each
manual runs live in [`scripts/`](../../scripts/), one folder per handheld; the
[index below](#the-scripts) says what every file does and which machine it runs on. Read a
script before you run it; most run as root and change the machine they run on.

## The scripts

[`scripts/`](../../scripts/) holds the device-side helpers, one folder per manual:
[`picocalc-lyra/`](../../scripts/picocalc-lyra/) for the PicoCalc — bringing up Calculinux on the Lyra,
building its console font, and (under [`xiao-radio/`](../../scripts/picocalc-lyra/xiao-radio/))
giving it a MeshCore radio over UART — and [`uconsole/`](../../scripts/uconsole/) for the
always-on bridge that fronts the uConsole's SPI-attached LoRa chip as a companion (MeshTerm
can also drive that chip directly, with no script at all — the uConsole manual covers
both). The manual is the
walkthrough; each script's own header says what it assumes about the machine and why each
step is the way it is. On the Lyra, only one program may hold `/dev/ttyS1` — MeshTerm
directly, or a pump in front of it, never both.

| File | Runs on | What it does |
| --- | --- | --- |
| [`picocalc-lyra/calculinux-setup.sh`](../../scripts/picocalc-lyra/calculinux-setup.sh) | the Lyra (root) | One-time bring-up: python packages, the Wi-Fi boot-scan kick, a boot clock sync (there is no RTC), the deploy user, the checkout, the venv and `pip install -e .`, the login PATH, and the console font. Idempotent. |
| [`picocalc-lyra/calculinux-console-font-6x12.sh`](../../scripts/picocalc-lyra/calculinux-console-font-6x12.sh) | the Lyra (root) | Just the console font: builds, installs and persists the 512-glyph **meshterm** PSF (braille, node marks, rounded frame corners, the list cursor) and restores the stock 16-slot palette. Calls the 6×8 build below when it sits alongside. Run it alone after a MeshTerm update, from the pulled checkout (`/home/meshterm/MeshTerm/scripts/picocalc-lyra/`) rather than the copy made at install time. |
| [`picocalc-lyra/calculinux-console-font-6x8.sh`](../../scripts/picocalc-lyra/calculinux-console-font-6x8.sh) | the Lyra (root) | The shorter-cell companion font, **meshterm8** (6×8 → 53×40), for anyone who wants more rows than the 6×12 default gives. Its base bitmap is the kernel's `font_6x8`, so this one file and the PSF it builds are **GPL-2.0-only** ([`LICENSE.GPL-2.0`](../../scripts/picocalc-lyra/LICENSE.GPL-2.0) sits beside it); it reads the donor/alias/keeper tables out of the script above rather than keeping a second copy. Pick between the two fonts from MeshTerm's Preferences page. |
| [`picocalc-lyra/calculinux-wifi-set.sh`](../../scripts/picocalc-lyra/calculinux-wifi-set.sh) | the Lyra (sudo) | Interactive Wi-Fi join: prompts for an SSID and passphrase, writes iwd's credentials, connects now. This is how a network *becomes* known to the boot kick. |
| [`picocalc-lyra/calculinux-radio-bridge.sh`](../../scripts/picocalc-lyra/calculinux-radio-bridge.sh) | the Lyra (root) | **Unfinished, unsupported.** An early UART-to-TCP pump: it serves `/dev/ttyS1` on TCP port 5000 and writes a `radio` TCP profile. It does not route UART1 to the header pads. Superseded by `xiao-radio/lyra-setup.sh`, which does the routing and points MeshTerm at the serial port directly. |
| [`picocalc-lyra/xiao-radio/`](../../scripts/picocalc-lyra/xiao-radio/) | see below | Everything for the XIAO nRF52840 + Wio-SX1262 radio wired to the Lyra's UART1; its [bench card](../../scripts/picocalc-lyra/xiao-radio/README.md) has the wiring table. The firmware is MeshCore, MIT-licensed — [`LICENSE.MeshCore`](../../scripts/picocalc-lyra/xiao-radio/LICENSE.MeshCore) sits beside the patch. |
| [`xiao-radio/build-firmware.sh`](../../scripts/picocalc-lyra/xiao-radio/build-firmware.sh) | dev machine | Clones and patches MeshCore, builds the `Xiao_nrf52_companion_radio_serial` environment, emits the `.uf2`. |
| [`xiao-radio/flash.py`](../../scripts/picocalc-lyra/xiao-radio/flash.py) | dev machine | Flashes the `.uf2` to the XIAO over its UF2 bootloader, refusing the wrong board or SoftDevice; prints the serial-DFU command when the bootloader offers no drive. |
| [`xiao-radio/meshcore-uart1.patch`](../../scripts/picocalc-lyra/xiao-radio/meshcore-uart1.patch) | — | The two firmware hunks: the companion on nRF52 `Serial1`, and I²C moved off the UART pads. Not upstream yet. |
| [`xiao-radio/lyra-setup.sh`](../../scripts/picocalc-lyra/xiao-radio/lyra-setup.sh) | the Lyra (root) | Boot-persistent UART1 → GP4/GP5 mux service, `dialout` membership, and a default MeshTerm serial profile for `/dev/ttyS1`. |
| [`xiao-radio/uart1-mux.py`](../../scripts/picocalc-lyra/xiao-radio/uart1-mux.py) | the Lyra (root) | The matrix-IO register poke that routes UART1 to the header pads, run once per boot by the service above. |
| [`uconsole/meshterm-spi-bridge`](../../scripts/uconsole/meshterm-spi-bridge) | the radio host (your user) | The **always-on** option: runs a mesh node in software over an SPI-attached SX1262 (uConsole AIO, Waveshare HATs) and serves the companion protocol on `127.0.0.1:5000`, so the node stays on the mesh while MeshTerm is closed. Setup menu, preflight, optional `systemd --user` service. Runs on `openhop_core` (preferred) or its predecessor `pymc_core`; see the manual for the venv it wants on a bookworm uConsole. Most people should reach for `meshterm --spi` instead — no script, no service, MeshTerm drives the chip itself while it runs. |

## The platforms

MeshTerm resolves one frozen **platform** at boot and draws everything through it. The
**regular** platform is a desktop or ssh terminal: 72 readable columns, truecolour, emoji,
a footer hint line. The **picocalc-lyra** platform is a framebuffer console: 53 columns, no
emoji, an F-key lane instead of a hint line, and sixteen colour slots rather than a colour
space. Nothing loses its colour there: the node hue and the recency heat gradient
quantise to those slots instead. It is detected from the device tree on the Lyra, and
`--platform picocalc-lyra` or `MESHTERM_PLATFORM=picocalc-lyra` forces it anywhere, which
is how you preview the handheld layout in your own terminal:

```bash
meshterm --mock --platform picocalc-lyra   # the whole app, simulated radio, handheld layout
meshterm specimen                          # the visual language on one card
meshterm platform                          # which platform this terminal resolved to
```

The **cardputer-zero** platform is the Cardputer Zero's 53×14 screen, which MeshTerm draws
itself. It is [not supported yet](cardputer-zero.md), so it is never detected: only
`--platform cardputer-zero` chooses it.

What that platform needs from the console — a font with the glyphs the interface draws,
and the stock sixteen-slot palette — is installed by the PicoCalc manual's setup script;
the details are in that manual and in the scripts' own headers.

## The emulator

`--platform` gives you a handheld's layout in your own terminal, drawn in your terminal's
font and colours. For a closer look, `meshterm emulate` opens a window showing MeshTerm the
way the handheld's own screen draws it:

```bash
meshterm emulate --fetch-fonts            # once: the font the handhelds draw in
meshterm emulate picocalc-lyra --mock     # the PicoCalc's 320×320 screen
meshterm emulate cardputer-zero --mock    # the Cardputer Zero's 320×170 screen
```

**It isn't a strict emulator.** It doesn't imitate the device's computer: MeshTerm runs on
your own machine, at your machine's speed. What it copies is the display's constraints —
the screen's size in pixels, the grid of letters on it, the font, the colours the device
can show, and the keys that drive the F-key lane. On the PicoCalc that means sixteen
colours, and bold drawn as a brighter colour rather than a heavier letter, the way its
console does it. Those are what decide whether a screen fits and reads well, so those are
what the window reproduces.

Your keyboard's F-keys stand in for the lane's keys: **F1** to **F5** for the PicoCalc's,
and **F4** to **F8** for the Cardputer Zero's **Fn+4** to **Fn+8**. Hold **Shift** for the
second set. You can also click them: click a coloured key at the bottom of the screen, or
one of the Cardputer Zero's keys drawn under it, and hold **Shift** while you click for the
second set. Everything you type after the device's name goes to the MeshTerm in the window:
`--mock` for the simulated radio, or `--port`, `--ble` or `--tcp` for a real one. `--scale`
sets the zoom, **Ctrl+Shift+S** saves a screenshot at the screen's real size, and closing
the window quits MeshTerm.

It needs MeshTerm installed with pip or pipx; the one-file downloads leave out the part of
Python that opens windows. The font is Terminus, which has its own licence, so MeshTerm
doesn't ship it: `--fetch-fonts` downloads the official release and checks it is the real
one. On a machine that can't download it, fetch `terminus-font-4.49.1.tar.gz` some other
way and install it with `meshterm emulate --archive terminus-font-4.49.1.tar.gz`.
