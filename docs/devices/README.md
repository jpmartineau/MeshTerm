# MeshTerm on hardware

MeshTerm talks to a **node**. The node is a MeshCore radio that speaks the companion
protocol over serial, Bluetooth LE, or TCP. If you plug a companion board into USB, or pair
one over Bluetooth, nothing on this page applies. MeshTerm finds the companion. For this
case, read the [README](../../README.md) and the [command line manual](../cli/README.md).

This page is for the hardware that does not present a node in that way. Each such handheld
has its own manual with numbered steps. This page tells you which manual is yours.

## Which guide is yours

| I have… | Read |
| --- | --- |
| a ClockworkPi **PicoCalc**, stock or already running Calculinux on a Luckfox Lyra, and I want MeshTerm on its own display | **[MeshTerm on the PicoCalc](picocalc-lyra.md)**: from the shopping list to a working machine |
| …and I want a **radio** inside it | the same manual, [Phase 3](picocalc-lyra.md#phase-3-add-the-radio): a XIAO nRF52840 + Wio-SX1262 soldered to the UART of the Lyra |
| a ClockworkPi **uConsole** with the hackergadgets AIO board (an SX1262 on the SPI bus of the host) | **[MeshTerm on the uConsole](uconsole.md)**: MeshTerm drives the radio directly with `--spi`. A bridge service can also present the radio as a companion, if you want it always on |
| an M5Stack **Cardputer Zero**, the one with a Raspberry Pi inside (not the ESP32 Cardputer or the Cardputer-Adv) | **[MeshTerm on the Cardputer Zero](cardputer-zero.md)**: ⚠️ **not supported yet**. MeshTerm has never run on the handheld. The page tells you what works in the emulator |
| a MeshCore companion on USB, Bluetooth, or Wi-Fi | nothing here. Read the [README](../../README.md). If the companion does not connect, read [When a companion does not connect](../guide/connecting.md) |

The handhelds have nothing in common except MeshTerm. Each manual runs scripts on the
handheld. The scripts are in [`scripts/`](../../scripts/), with one folder for each
handheld. The [index below](#the-scripts) tells you what each file does and which machine
runs it. Read a script before you run it. Most scripts run as root and change the machine
that runs them.

## The scripts

[`scripts/`](../../scripts/) has the helper scripts that run on the handheld. There is one
folder for each manual:

- [`picocalc-lyra/`](../../scripts/picocalc-lyra/) is for the PicoCalc. It has the scripts
  that bring up Calculinux on the Lyra and build the console font. Under
  [`xiao-radio/`](../../scripts/picocalc-lyra/xiao-radio/), it also has the scripts that
  give the PicoCalc a MeshCore radio over UART.
- [`uconsole/`](../../scripts/uconsole/) has the always-on bridge. The bridge presents the
  LoRa chip on the SPI bus of the uConsole as a companion. MeshTerm can also drive that chip
  directly, with no script. The uConsole manual describes both ways.

The manual is the walkthrough. The header of each script says what the script assumes about
the machine, and why each step is as it is. On the Lyra, only one program can hold
`/dev/ttyS1`. It is MeshTerm directly, or a pump in front of MeshTerm, but never both.

| File | Runs on | What it does |
| --- | --- | --- |
| [`picocalc-lyra/calculinux-setup.sh`](../../scripts/picocalc-lyra/calculinux-setup.sh) | the Lyra (root) | One-time bring-up. It sets up the python packages, the Wi-Fi boot-scan kick, a clock sync at boot (there is no RTC), the deploy user, the checkout, the venv and `pip install -e .`, the login PATH, and the console font. You can run it again without a problem. |
| [`picocalc-lyra/calculinux-console-font-6x12.sh`](../../scripts/picocalc-lyra/calculinux-console-font-6x12.sh) | the Lyra (root) | Only the console font. It builds, installs, and keeps the 512-glyph **meshterm** PSF (braille, node marks, rounded frame corners, and the list pointer). It also restores the stock 16-slot palette. It calls the 6×8 build below when that script is in the same folder. After a MeshTerm update, run it alone from the pulled checkout (`/home/meshterm/MeshTerm/scripts/picocalc-lyra/`). Do not run the copy that the install made. |
| [`picocalc-lyra/calculinux-console-font-6x8.sh`](../../scripts/picocalc-lyra/calculinux-console-font-6x8.sh) | the Lyra (root) | The companion font with shorter cells, **meshterm8** (6×8, which gives 53×40). Use it if you want more rows than the 6×12 default gives. Its base bitmap is the `font_6x8` of the kernel. Thus this file and the PSF that it builds are **GPL-2.0-only**. The file [`LICENSE.GPL-2.0`](../../scripts/picocalc-lyra/LICENSE.GPL-2.0) is next to it. The script reads the donor, alias, and keeper tables from the script above and does not keep a second copy. Select between the two fonts on the Preferences page of MeshTerm. |
| [`picocalc-lyra/calculinux-wifi-set.sh`](../../scripts/picocalc-lyra/calculinux-wifi-set.sh) | the Lyra (sudo) | Interactive Wi-Fi join. It asks for an SSID and a passphrase, writes the credentials of iwd, and connects at once. This is how a network becomes known to the boot kick. |
| [`picocalc-lyra/calculinux-radio-bridge.sh`](../../scripts/picocalc-lyra/calculinux-radio-bridge.sh) | the Lyra (root) | **Unfinished and not supported.** An early UART-to-TCP pump. It serves `/dev/ttyS1` on TCP port 5000 and writes a `radio` TCP profile. It does not route UART1 to the header pads. `xiao-radio/lyra-setup.sh` replaces it. That script does the routing and points MeshTerm at the serial port directly. |
| [`picocalc-lyra/xiao-radio/`](../../scripts/picocalc-lyra/xiao-radio/) | see below | Everything for the XIAO nRF52840 + Wio-SX1262 radio that is wired to UART1 of the Lyra. Its [bench card](../../scripts/picocalc-lyra/xiao-radio/README.md) has the wiring table. The firmware is MeshCore, with the MIT licence. The file [`LICENSE.MeshCore`](../../scripts/picocalc-lyra/xiao-radio/LICENSE.MeshCore) is next to the patch. |
| [`xiao-radio/build-firmware.sh`](../../scripts/picocalc-lyra/xiao-radio/build-firmware.sh) | development machine | Clones and patches MeshCore, builds the `Xiao_nrf52_companion_radio_serial` environment, and makes the `.uf2` file. |
| [`xiao-radio/flash.py`](../../scripts/picocalc-lyra/xiao-radio/flash.py) | development machine | Flashes the `.uf2` file to the XIAO through its UF2 bootloader. It refuses the wrong board or SoftDevice. When the bootloader gives no drive, it prints the serial-DFU command. |
| [`xiao-radio/meshcore-uart1.patch`](../../scripts/picocalc-lyra/xiao-radio/meshcore-uart1.patch) | none | The two firmware hunks: the companion on `Serial1` of the nRF52, and I²C moved off the UART pads. It is not upstream yet. |
| [`xiao-radio/meshcore-frame-timeout.patch`](../../scripts/picocalc-lyra/xiao-radio/meshcore-frame-timeout.patch) | none | The serial parser of the companion drops a frame whose bytes stop for 200 ms. Thus one lost byte cannot make the radio ignore each command after it. It is not upstream yet. |
| [`xiao-radio/lyra-setup.sh`](../../scripts/picocalc-lyra/xiao-radio/lyra-setup.sh) | the Lyra (root) | A UART1 → GP4/GP5 mux service that stays after a reboot, membership of `dialout`, and a default MeshTerm serial profile for `/dev/ttyS1`. |
| [`xiao-radio/uart1-mux.py`](../../scripts/picocalc-lyra/xiao-radio/uart1-mux.py) | the Lyra (root) | The matrix-IO register poke that routes UART1 to the header pads. The service above runs it once at each boot. |
| [`uconsole/meshterm-spi-bridge`](../../scripts/uconsole/meshterm-spi-bridge) | the radio host (your user) | The **always-on** option. It runs a mesh node in software over an SX1262 on an SPI bus (uConsole AIO, Waveshare HATs). It serves the companion protocol on `127.0.0.1:5000`. Thus the node stays on the mesh while MeshTerm is closed. It has a setup menu, a preflight check, and an optional `systemd --user` service. It runs on `openhop_core` (preferred) or on its predecessor `pymc_core`. The manual describes the venv that it needs on a bookworm uConsole. Most users must use `meshterm --spi` instead. It needs no script and no service, and MeshTerm drives the chip itself while MeshTerm runs. |

## The platforms

MeshTerm resolves one frozen **platform** at boot and draws everything through it. The
**regular** platform is a desktop terminal or an ssh terminal. It has 72 readable columns,
truecolour, emoji, and a footer hint line.

The **picocalc-lyra** platform is a framebuffer console. It has 53 columns and no emoji.
It has an F-key lane, not a hint line. It has sixteen colour slots, not a colour space.
No element loses its colour there. The hue of a node and the heat gradient of recency are
quantized to those slots instead. MeshTerm detects this platform from the device tree on
the Lyra. You can force it on any machine with `--platform picocalc-lyra` or
`MESHTERM_PLATFORM=picocalc-lyra`. With this, you can preview the layout of the handheld
in your own terminal:

```bash
meshterm --mock --platform picocalc-lyra   # the whole app, simulated radio, handheld layout
meshterm specimen                          # the visual language on one card
meshterm platform                          # which platform this terminal resolved to
```

The **cardputer-zero** platform is the display of the Cardputer Zero, which is 53×14.
MeshTerm draws this display itself. The platform is [not supported yet](cardputer-zero.md),
thus MeshTerm never detects it. Only `--platform cardputer-zero` selects it.

The platform needs two things from the console: a font that has the glyphs that the
interface draws, and the stock sixteen-slot palette. The setup script in the PicoCalc
manual installs both. The manual and the headers of the scripts give the details.

## The emulator

`--platform` gives you the layout of a handheld in your own terminal. MeshTerm draws it in
the font and the colours of your terminal. For a closer look, run `meshterm emulate`. It
opens a window that shows MeshTerm as the display of the handheld draws it:

```bash
meshterm emulate --fetch-fonts            # once: the font the handhelds draw in
meshterm emulate picocalc-lyra --mock     # the PicoCalc's 320×320 screen
meshterm emulate cardputer-zero --mock    # the Cardputer Zero's 320×170 screen
```

**The emulator is not a strict emulator.** It does not copy the computer of the handheld.
MeshTerm runs on your own machine, at the speed of your machine. The emulator copies the
constraints of the display and the keyboard:

- the size of the display in pixels
- the grid of letters on the display
- the font
- the colours that the handheld can show
- the keys that drive the F-key lane

On the PicoCalc, this means sixteen colours. It also means that bold text is a brighter
colour and not a heavier letter, as on the console of the PicoCalc. These constraints
decide if a screen fits and is easy to read. Thus the window reproduces them.

The F-keys of your keyboard replace the keys of the lane. Use **F1** to **F5** for the
PicoCalc. Use **F4** to **F8** for **Fn+4** to **Fn+8** on the Cardputer Zero. Hold
**Shift** for the second set. You can also click the keys. Click a coloured key at the
bottom of the display, or one of the keys of the Cardputer Zero that are drawn under it.
Hold **Shift** while you click for the second set.

Each argument that you type after the name of the handheld goes to the MeshTerm in the
window. Use `--mock` for the simulated radio. Use `--port`, `--ble`, or `--tcp` for a real
one. `--scale` sets the zoom. **Ctrl+Shift+S** saves a screenshot at the real size of the
display. When you close the window, MeshTerm quits.

The emulator needs MeshTerm installed with pip or pipx. The one-file downloads do not have
the part of Python that opens windows. The font is Terminus, which has its own licence.
Thus MeshTerm does not include it. `--fetch-fonts` downloads the official release and
checks that it is the real one. If your machine cannot download it, download
`terminus-font-4.49.1.tar.gz` in another way. Then install it with
`meshterm emulate --archive terminus-font-4.49.1.tar.gz`.
