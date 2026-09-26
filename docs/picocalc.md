# MeshTerm on the PicoCalc

You start with a stock ClockworkPi PicoCalc — the kit that ships with a Raspberry Pi Pico —
and a short shopping list. You end with a Linux handheld: MeshTerm running full-screen on
the PicoCalc's own display and keyboard, a LoRa radio soldered inside, and no laptop needed
to use it day to day. There are three phases: build the machine (swap the Pico for a Linux
board), install the OS and MeshTerm, and add the radio. Budget an afternoon for the first
two phases, most of it spent waiting for a toolchain to download; the radio phase needs a
soldering iron and can be done later — MeshTerm's `--mock` mode runs perfectly well without
a radio in the meantime.

> ⚠ **Read this first.** This build takes the PicoCalc apart, swaps its core board, and
> solders to it. It assumes you can already solder cleanly, use a multimeter, and work with
> small electronics without forcing anything. There is a real risk of damaging the
> hardware if a step goes wrong — a cracked screen, a snapped shell post, a dead board, or
> an overcharged lithium cell — and some of that damage is not repairable. You do this at
> your own risk.
>
> **Read the whole manual through once before you buy anything or open anything.** Several
> steps only make sense in light of a later one — where the SD card goes, what must be out
> of the case before a board is pressed in, which USB port is never to be powered — and the
> shopping list is easier to get right when you know what each part is for.
>
> This is a guide, not a prescription. Despite the author's best efforts it may contain
> errors, and parts of it will go out of date as boards, images and firmware change. It is
> up to you to check every fact in it against your own hardware and the current sources
> before you act on it, and every decision along the way is yours. The author accepts no
> responsibility for any damage, loss, or injury that results from following this guide,
> and nothing in it is a warranty that your parts, your tools, or your hands will behave the
> way the author's did. If you are not comfortable with any step, stop and get help from
> someone who is.

- [Shopping list](#shopping-list)
- [Before you start](#before-you-start)
- [Three ways to break it](#three-ways-to-break-it)
- [Phase 1 — Build the machine](#phase-1--build-the-machine)
  - [Step 1 — Assemble the PicoCalc](#step-1--assemble-the-picocalc)
  - [Step 2 — Swap the Pico for the Lyra](#step-2--swap-the-pico-for-the-lyra)
  - [Step 3 — Attach the Wi-Fi dongle](#step-3--attach-the-wi-fi-dongle)
- [Phase 2 — Install Calculinux and MeshTerm](#phase-2--install-calculinux-and-meshterm)
  - [Step 4 — Write the image to the microSD](#step-4--write-the-image-to-the-microsd)
  - [Step 5 — First boot](#step-5--first-boot)
  - [Step 6 — Open the serial console](#step-6--open-the-serial-console)
  - [Step 7 — Join Wi-Fi](#step-7--join-wi-fi)
  - [Step 8 — Copy the setup scripts to the device](#step-8--copy-the-setup-scripts-to-the-device)
  - [Step 9 — Run calculinux-setup.sh](#step-9--run-calculinux-setupsh)
  - [Step 10 — Log in as meshterm](#step-10--log-in-as-meshterm)
- [Phase 3 — Add the radio](#phase-3--add-the-radio)
  - [Step 11 — Install PlatformIO](#step-11--install-platformio)
  - [Step 12 — Build the firmware](#step-12--build-the-firmware)
  - [Step 13 — Flash the XIAO](#step-13--flash-the-xiao)
  - [Step 14 — Wire the radio](#step-14--wire-the-radio)
  - [Step 15 — Set up the Lyra](#step-15--set-up-the-lyra)
  - [Step 16 — Run it](#step-16--run-it)
- [Day-to-day](#day-to-day)
- [Troubleshooting](#troubleshooting)
- [What has been tested](#what-has-been-tested)

---

## Shopping list

### The machine

| Item | Product / what to search for | Approx. price | Link | Note |
| --- | --- | --- | --- | --- |
| PicoCalc kit | ClockworkPi PicoCalc | $89 | [clockworkpi.com/product-page/picocalc](https://www.clockworkpi.com/product-page/picocalc) | Comes with a Raspberry Pi Pico 1H, screen, keyboard, shell, hex key and a 32 GB SD card. **18650 batteries are not included.** |
| Luckfox Lyra | "Luckfox Lyra 128MB, pre-soldered header, no NAND" | $20–25 | [luckfox.com/Luckfox-Lyra](https://www.luckfox.com/Luckfox-Lyra) | Buy the **plain 128 MB RAM, Pico-form-factor** variant, **with the header pre-soldered**. Other RAM sizes have incompatible pinouts, so this is the one part of the list where the wrong SKU won't work. |
| microSD card | 16 GB+, Class 10 or better | $6–10 | — | The PicoCalc's stock 32 GB card works fine — it's more than the 8 GB minimum. You can reuse it instead of buying a new one. |
| An 18650 battery | e.g. Samsung 30Q, Molicel P26A, Sony VTC6 (examples, not endorsements) | $5–10 | — | **Unprotected**, flat-top or button-top, Ø18 × 65–69 mm. One is enough to run; a second is optional (below). See [the battery note](#a-note-on-batteries) below. |
| USB Wi-Fi dongle | TP-Link TL-WN725N (an RTL8188EU nano dongle), or search the **chipset name**: RTL8192CU, R8712U, RTL8188EU | $5–10 | [amazon.ca/dp/B008IFXQFU](https://www.amazon.ca/dp/B008IFXQFU) | The TL-WN725N is the dongle this guide was built with. Must run at **3.3 V** — see [Step 3](#step-3--attach-the-wi-fi-dongle). Only needed for setup and updates; MeshTerm itself runs offline. |
| MX1.25 4-pin pigtail | "MX1.25 4P cable with leads" (a pack of pre-crimped pigtails, 10–15 cm) | $2–4 | — | One end of the DIY USB lead in [Step 3](#step-3--attach-the-wi-fi-dongle). Pre-crimped, because MX1.25 contacts need a crimper you don't otherwise own. |
| USB-A female socket | "USB 2.0 type A female socket, solder / through-hole" | $2–3 | — | The other end of the DIY lead. A ready-made [MX1.25 4P to USB-A cable](https://spotpear.com/shop/Luckfox-Lyra-MX1.25-4P-To-USB-A-Cable.html) (about $1, 30 cm) works too, but it is a lot of cable to fold into the shell. |

### The radio

| Item | Product / what to search for | Approx. price | Link | Note |
| --- | --- | --- | --- | --- |
| XIAO nRF52840 + Wio-SX1262 kit | "XIAO nRF52840 & Wio-SX1262 Kit for Meshtastic", SKU 102010710 | $13.49 | [seeedstudio.com](https://www.seeedstudio.com/XIAO-nRF52840-Wio-SX1262-Kit-for-Meshtastic-p-6400.html) | Includes the XIAO nRF52840, the Wio-SX1262 LoRa board (stacked and wired together) and an antenna. Covers both 868 and 915 MHz. Plain or Sense XIAO both work. **Do not** buy the "-N" variant — it has no antenna connector. |

### Tools and consumables

| Item | Note |
| --- | --- |
| Soldering iron, fine tip, and solder | For the four radio wires. |
| Hookup wire, 26–30 AWG, four short lengths | TX and RX get crossed — see [Step 14](#step-14--wire-the-radio). |
| 2.5 mm hex key | Comes with the PicoCalc kit. |
| USB-C data cable | For the serial console, and later for flashing the XIAO. A charge-only cable won't do; it has to carry data. |
| microSD card reader | For writing the Calculinux image from your PC. |
| Small side cutters | For trimming wire ends, and a section of the case grille if the Wi-Fi lead needs the room. |
| Small needle file | To open the back panel's USB-C cutout a little if the Lyra's connector doesn't clear it. |
| Kapton (polyimide) tape | Wraps the XIAO stack and the DIY USB lead so nothing bare touches the board or the shell. Heat-proof, thin, and it peels off cleanly. |
| A PC (Windows/macOS/Linux) with Python 3 and git | Runs the image-writing, firmware-build and flashing steps. |

### Optional

| Item | Why |
| --- | --- |
| A second 18650 battery | The holder takes two; one is enough to run, two roughly double your runtime. |
| Powered USB hub | If you want the Wi-Fi dongle and something else on the Lyra's single USB host socket at once, or a 5 V dongle. |
| Foam tape | To secure the XIAO board inside the shell once it's wired in. |

Approximate total: **$130–150** for the machine and radio together, before batteries — most
of that is the PicoCalc kit ($89), the Lyra (~$22) and the radio kit ($13.49).

**What you do NOT need:**

- A level shifter. Every signal in this build — the Lyra's header, the XIAO, the SX1262 — is
  3.3 V.
- A separate USB-to-serial adapter. The PicoCalc mainboard already has one built in.
- Bluetooth anything. Neither the Lyra nor this radio path uses it.
- A new SD card, if your stock 32 GB card is handy — it's well over the 8 GB minimum
  Calculinux asks for.

### A note on batteries

ClockworkPi has not published an official battery spec for the PicoCalc — a
[still-open GitHub issue](https://github.com/clockworkpi/PicoCalc/issues/6) asking for one
has gone unanswered. What's known from the kit itself: the holder takes two 18650 cells, one
cell is enough to power the board, and the physical envelope is about Ø18 mm × 65–69 mm,
which rules out most *protected* cells — the protection circuit adds length that doesn't
fit. Unprotected flat-top or button-top cells both work; the PicoCalc mainboard has its own
charge circuit built in. Buy from a reputable brand rather than a cheap unbranded listing —
Samsung 30Q, Molicel P26A and Sony VTC6 are commonly recommended examples, not an
endorsement of any one of them.

---

## Before you start

Work happens on two machines, and it helps to keep straight which is which:

- **Your PC** writes the Calculinux image to the microSD card, builds the radio firmware,
  and flashes the XIAO. Nothing in these three jobs touches the PicoCalc itself.
- **The PicoCalc** (really, the Luckfox Lyra inside it) does everything else — setup,
  Wi-Fi, running MeshTerm — either over the serial console from your PC or on its own
  screen and keyboard. Both work identically; the serial console is just easier for pasting
  long commands.

The device has two logins you'll use for different things: **`root`**, which you use once
to run the setup script and fix Wi-Fi, and **`meshterm`**, the unprivileged account
MeshTerm actually runs under day to day.

Calculinux is a console-only Linux — no desktop, no window manager. That's deliberate: the
PicoCalc's screen and keyboard are a terminal, and MeshTerm is a full-screen terminal
program built for exactly that.

---

## Three ways to break it

The PicoCalc is a kit, and its forum has a well-worn list of the ways people have damaged
theirs. Three of them apply directly to this build, and the first two are not theoretical:
the author of this guide **cracked a screen and snapped screw posts** building the very
machine it describes. Read this section before you open the shell.

> ⚠ **The screen is extremely fragile.** It is a bare glass panel with no frame of its own,
> and when the case is closed it sits directly against the mainboard. Anything that flexes
> the mainboard flexes the glass: pushing a board into the Pico socket, prying one out,
> closing the shell with the screen not sitting square in its opening, or pressing a key
> while the halves are being screwed together. Forum members have cracked screens doing
> each of those, and the swap in [Step 2](#step-2--swap-the-pico-for-the-lyra) is the
> classic one — pressing a new board into the socket with the mainboard still in the case
> is enough on its own. That is exactly how the screen in this guide's own PicoCalc broke.
> ([Before replacing the Pico](https://forum.clockworkpi.com/t/before-replacing-the-pico-read-this-to-avoid-cracked-screen/16666),
> [Avoid breaking your screen](https://forum.clockworkpi.com/t/avoid-breaking-your-screen/18805))
>
> What to do instead:
>
> - **Never push a board into the socket, or pull one out, with the mainboard in the case.**
>   Unplug the display's ribbon cable (the FPC) from the mainboard, lift the mainboard out,
>   and do the swap on a flat, padded surface — a folded microfibre cloth is ideal. Ease a
>   board out a little at each end in turn, never by levering one end.
> - **Secure the screen with Kapton tape** along its edges so it cannot shift in the
>   opening while you work and while the case is closed. Kapton is the right tape: thin,
>   it stays put, and it peels off cleanly. Don't use anything that sets hard.
> - **Before the back goes on, check the screen sits flush and square** in the front half's
>   opening with no pressure on any corner. If the mainboard doesn't sit perfectly flush on
>   the front half, stop and find out why before you add the back.
> - Handle the panel as little as possible, and keep your fingers off the keyboard while
>   closing the shell.

> ⚠ **Don't overtighten the screws.** The shell's screw posts are brass inserts moulded
> into plastic, and forcing a screw snaps the post out of the shell — a repair that needs
> epoxy and never quite comes back
> ([Broken screw thread](https://forum.clockworkpi.com/t/broken-screw-thread/20726)).
> This guide's author snapped several on the first assembly; it takes less force than you
> would think.
> Turn each screw until it just seats, then stop. If a screw won't go in easily, the halves
> aren't aligned, or something inside is in the way; more force is never the answer.

> ⚠ **Never power the Lyra through its own USB-C while the batteries are in.** The
> PicoCalc charges its cells only through the **mainboard's** USB-C port, under the control
> of its power-management chip (an AXP2101). Power fed in through the core board's USB port
> — the Pico's originally, the Lyra's now — arrives on the header's system-power pin and
> bypasses that charge control. Forum members who powered the Pico's port directly measured
> their cells at **4.46 V and 4.74 V** afterwards, well over the 4.2 V a lithium cell is
> allowed to reach
> ([New PicoCalc — battery charging](https://forum.clockworkpi.com/t/new-picocalc-battery-charging/22330)).
> An overcharged cell is a fire risk, not just a worn one.
>
> Nothing in this guide needs the Lyra's USB-C: the serial console, charging and power all
> use the mainboard's port on the side of the case, and the Wi-Fi dongle uses the 4-pin
> socket on top of the Lyra. If you ever must plug the Lyra's USB-C into a PC — to
> reflash it with Luckfox's tools, say — **take both batteries out first.**

---

## Phase 1 — Build the machine

### Step 1 — Assemble the PicoCalc

Follow ClockworkPi's own
[assembly guidelines](https://github.com/clockworkpi/PicoCalc/blob/master/Clockwork_PicoCalc_Assembly_Guidelines.pdf)
(a PDF in the [PicoCalc repository](https://github.com/clockworkpi/PicoCalc), which also
has a wiki) to put the kit together: mainboard, screen, keyboard, speakers, and shell. Insert your 18650
battery (or two), watching the `+`/`-` marks in the battery compartment.

⚠ Two of the warnings in [Three ways to break it](#three-ways-to-break-it) apply here: tape
the screen's edges with Kapton before you close the shell and check it sits square, and
turn the screws only until they seat. You will open the shell again in the next step, so
there is no reason to make the screws tight now.

Power it on once with the **stock Pico still in place**. You should see the stock BASIC
firmware boot on the screen, and the keyboard should respond. This confirms the screen,
keyboard, and battery are all good before you start swapping boards.

Power it back off.

### Step 2 — Swap the Pico for the Lyra

Remove the back of the shell with the hex key.

⚠ **Do the swap with the mainboard out of the case.** This is the step that cracks screens
(see [Three ways to break it](#three-ways-to-break-it)). Unplug the display's ribbon cable
from the mainboard, lift the mainboard out, and lay it on a padded surface. Only then ease
the Raspberry Pi Pico out of its socket, a little at each end in turn — **keep it**, you're
just setting it aside.

If your Luckfox Lyra has SPI NAND on board (a "Lyra B"), erase it first — the boot ROM
otherwise ignores the SD card entirely and boots the on-board flash instead. See
[Calculinux's hardware requirements page](https://calculinux.org/getting-started/hardware-requirements/)
for how.

Seat the Lyra into the same socket the Pico came out of, in the same orientation — its
USB-C port at the end where the Pico's USB port was, so it lines up with the cutout in the
back of the shell. Press it down fully with the mainboard still on the padded surface; a
Lyra that isn't fully seated won't boot. Then reconnect the ribbon cable and set the
mainboard back into the front half, checking the screen sits square before anything is
screwed down.

You should see the Lyra sitting flat on the socket with no gap at either end, and, once
the mainboard is back in, its USB-C port centred in the back-panel cutout.

The cutout was made for the Pico's micro-USB, and the Lyra's USB-C connector is a
different shape. Depending on the connector Luckfox fitted to your board, the back panel
may not close over it. If it catches, open the hole a little with a small needle file,
testing the fit as you go, until the panel sits down without pressing on the board.

Leave the back off for now. The microSD card goes into the **Lyra's own slot** in
[Step 4](#step-4--write-the-image-to-the-microsd), and that slot is easier to reach with the
board exposed.

### Step 3 — Attach the Wi-Fi dongle

The Lyra has two USB connectors. The USB-C on its edge is for power and flashing and is
not used here. The **tiny 4-pin socket on top of the board** (an MX1.25 connector, next to
the two buttons by the USB-C end) is the USB host. The Wi-Fi dongle hangs off it through a
short lead you make yourself: a ready-made cable is 30 cm long and a struggle to fold into
the shell, and you have the soldering iron out for the radio anyway.

**Make the lead.** Solder the four wires of an MX1.25 pigtail to a USB-A female socket,
keeping the run short — 5 to 8 cm is enough to reach a spot where the dongle sits flat.
The pigtail's pins are in the standard USB order, which the pre-made cable's wire colours
follow:

| MX1.25 pin | USB-A socket pin | Signal | Pigtail wire colour (usually) |
| --- | --- | --- | --- |
| 1 | 1 | VBUS (**3.3 V** on this board) | red |
| 2 | 2 | D− | white |
| 3 | 3 | D+ | green |
| 4 | 4 | GND | black |

USB-A socket pins are numbered 1 to 4 across the connector's opening: VBUS on one edge,
GND on the other, the data pair between them. Colours on a pigtail are a convention, not a
promise, so check pin 1 before you plug anything in: with the Lyra powered, the socket's
pin 1 reads **3.3 V** against a GND header pin and the pin at the other end reads 0 V.
Swapping VBUS and GND is the one mistake that damages the dongle.

Once soldered, wrap the socket's solder side and the exposed wire in Kapton tape. Plug the
MX1.25 end into the socket on the Lyra — it is keyed and only goes in one way — and the
Wi-Fi dongle into the USB-A end. Lead and dongle stay inside the shell; tuck them where
they don't press on the board.

The MX1.25 socket sits on top of the Lyra, and the lead leaves it upward, which is where
the back panel's grille is. The wire may interfere with the grille when the back goes on.
If it does, snip a small section out of the grille with the side cutters where the wire
passes and shape the edge until nothing presses on the plug or the wire. Better to remove a
sliver of plastic than to close the case against a connector.

> **The Lyra's USB port is 3.3 V, not 5 V.** A standard 5 V USB Wi-Fi dongle will not work
> and may damage the board. Stick to the tested chipsets — RTL8192CU, R8712U, or RTL8188EU
> — which is why the shopping list names a known dongle (the TP-Link TL-WN725N) and
> otherwise tells you to search by chipset rather than by brand.

Don't power the device on yet — Wi-Fi is set up later, once Calculinux is installed.

---

## Phase 2 — Install Calculinux and MeshTerm

### Step 4 — Write the image to the microSD

On your PC, download `calculinux-image-luckfox-lyra.rootfs-<timestamp>.wic.gz` from the
newest release on the
[Calculinux releases page](https://github.com/Calculinux/meta-calculinux/releases). Every
Calculinux release is marked **Pre-release**, so there is no "Latest" badge to follow; take
the newest pre-release.

Decompress it:

```bash
# on the PC
gunzip calculinux-image-luckfox-lyra.rootfs-*.wic.gz
```

(The Calculinux installation page mentions `unxz`, but the file is a `.gz`, so use
`gunzip`. 7-Zip works on any platform if you'd rather not use the command line.)

Write the resulting `.wic` file to your microSD card. Three options:

- **Balena Etcher** (recommended, all platforms) — open it, select the `.wic` file, select
  your SD card, click Flash.
- **`dd`**, on Linux/macOS. Check the device name first — writing to the wrong device
  destroys its contents:

  ```bash
  # on the PC
  lsblk                                        # find your SD card's device, e.g. /dev/sdb
  sudo dd if=calculinux-image-luckfox-lyra.rootfs-*.wic of=/dev/sdX bs=4M status=progress conv=fsync
  ```

- **Rufus**, on Windows — select the device and the image, choose the **MBR** partition
  scheme, and start.

This erases everything already on the card. Once it finishes, eject it and put it in the
**Luckfox Lyra's own microSD slot** — not the PicoCalc's front-facing slot. The Lyra boots
from its own slot; the front slot is only reachable once MeshTerm (or anything else) is
already running on Linux.

Leave the front slot **empty** for the first boot, as Calculinux's own instructions warn: a
second card there with duplicate partition labels leaves the system read-only.

### Step 5 — First boot

With the card in the Lyra, power the PicoCalc on. Give it 30–60 seconds — the first boot is
slower than later ones. You should see boot messages scroll on the PicoCalc's own screen,
ending in a `Calculinux GNU/Linux` login prompt.

Log in:

```
login: root
Password: root
```

Change the password immediately:

```bash
# on the PicoCalc, as root
passwd
```

### Step 6 — Open the serial console

The PicoCalc's own screen and keyboard work fine, so you can skip this step. The serial
console makes pasting the longer commands in this guide much easier, though, so it is
recommended.

The PicoCalc mainboard's USB-C port carries a USB-to-UART bridge (a CH340 chip). Plug a
USB-C cable from that port into your PC. It shows up as:

- `/dev/ttyUSB0` on Linux (you may need to be in the `dialout` group),
- a `COM` port on Windows,
- `/dev/cu.usbserial-*` on macOS.

Settings: **1500000 8N1**, no flow control. Using `pyserial`'s `miniterm`:

```bash
# on the PC
pip install pyserial
python3 -m serial.tools.miniterm /dev/ttyUSB0 1500000
```

minicom and PuTTY work too, at the same settings. To leave `miniterm`, press **Ctrl+]**.

You should see the same login prompt you saw on the PicoCalc's screen, now in your PC's
terminal.

### Step 7 — Join Wi-Fi

Calculinux manages Wi-Fi with `iwd`. The easiest way is its text-mode picker: run `uwific`,
highlight your network, press Enter and type the passphrase (`Q` quits). Or do the same
from the command line:

```bash
# on the PicoCalc, as root
iwctl station wlan0 scan
iwctl station wlan0 get-networks
iwctl station wlan0 connect "<your SSID>"
```

It'll prompt for the passphrase. Check you're online:

```bash
ip addr show wlan0
ping -c 3 1.1.1.1
```

You should see an `inet` address on `wlan0`, and replies from the ping. `iwd` remembers
the network, so this is a one-time step; to change networks later, run `uwific` again.

### Step 8 — Copy the setup scripts to the device

You need this repository's `scripts/` directory on the Lyra — its `picocalc/` folder is the
part the Lyra runs. Two ways to get it there, once Wi-Fi is up:

- **`scp` from your PC**. Find the Lyra's IP address with `ip addr show wlan0` on the
  device, then from your PC:

  ```bash
  # on the PC, from your MeshTerm checkout
  scp -r scripts root@<lyra-ip>:/root/
  ```

  If root is refused over SSH, copy as the `pico` user (password `calc`) instead, then
  move the directory as root on the device:

  ```bash
  # on the PC
  scp -r scripts pico@<lyra-ip>:/home/pico/
  # on the PicoCalc, as root
  mv /home/pico/scripts /root/
  ```

- **`git clone` on the device.** `git` ships in the Calculinux base image, so as root you
  can run `git clone https://github.com/jpmartineau/MeshTerm` and use the `scripts/`
  folder inside the clone.

`scp` is the recommended route.

### Step 9 — Run calculinux-setup.sh

```bash
# on the PicoCalc, as root, with scripts/ copied over
cd scripts/picocalc
sh calculinux-setup.sh
```

It's idempotent — safe to re-run — and it checks before it acts. It reads these
environment variables at the top of the script:

| Knob | Default | Effect |
| --- | --- | --- |
| `DEPLOY_USER` | `meshterm` | the login MeshTerm runs under |
| `TIMEZONE` | `America/Toronto` | any IANA zone. Set it if you are not on Eastern time. |
| `REPO_URL` | the project's HTTPS URL | cloned when there's no deploy key |
| `REPO_SSH` | the project's SSH URL | cloned when there is one |
| `KEY_PATH` | `/home/$DEPLOY_USER/.ssh/id_ed25519` | a read-only GitHub deploy key, if you want SSH instead of HTTPS |
| `WIFI_SSID`, `WIFI_PSK` | unset | set both and the script writes `iwd`'s credentials for that network, as an alternative to Step 7 |

The defaults clone MeshTerm's public repository over plain HTTPS, which needs no credential
at all. The one you are likely to need is `TIMEZONE`, if you live outside Eastern time:

```bash
# on the PicoCalc, as root
TIMEZONE=America/Vancouver sh calculinux-setup.sh
```

The script installs only the Americas time-zone data, so a zone elsewhere may not take. If
it cannot set the zone, it warns and the clock stays on UTC.

What it does, one line per phase:

- **opkg packages** — installs `python3-modules` and `python3-pip` (and `git` and `kbd`,
  if the image lacks them), because Calculinux ships a stripped Python missing `pip`,
  `venv` and several stdlib modules.
- **Wi-Fi kick** — installs a boot-time service that re-scans until the dongle joins the
  network `iwd` already knows, because on a cold boot the dongle's firmware finishes
  loading after `iwd`'s first scan and the link would otherwise never come up.
- **Time sync** — installs a boot-time service that sets the clock over the network, since
  this board has no battery-backed real-time clock.
- **Timezone** — sets `$TIMEZONE` (`America/Toronto` by default).
- **Deploy user** — creates the `meshterm` login and puts it in the groups it needs.
- **Clone** — checks out MeshTerm to `/home/meshterm/MeshTerm`.
- **venv and install** — builds a Python virtual environment and `pip install -e .`s
  MeshTerm into it.
- **Login PATH** — puts that venv's `bin` on the `meshterm` user's PATH.
- **Console font** — builds and installs the console font MeshTerm's UI needs (braille
  charts, node glyphs, rounded corners).

This takes a few minutes on a good connection — most of it is `pip install` compiling
things. It ends with `done` and tells you to log in as `meshterm`. That user has no
password yet, so set one first:

```bash
# on the PicoCalc, as root
passwd meshterm
```

> **With no Wi-Fi dongle attached, the next boot can take up to about eleven minutes.**
> The Wi-Fi kick keeps trying for about six before it gives up, and the time sync then
> waits up to five more for a network route. Nothing is broken. If the device is going to
> live offline, see [Day-to-day](#day-to-day) for how to switch both off.

### Step 10 — Log in as meshterm

Log out of `root`, and log in as `meshterm` with the password you just set. Try the app:

```bash
# on the PicoCalc, as meshterm
meshterm --mock
```

You should see MeshTerm's full-screen interface come up at **53 columns**, with a simulated
radio behind it, drawing braille charts and node glyphs cleanly — no empty tofu boxes. Along
the bottom you should see the **F-key lane**: five labelled chips instead of the footer hint
line a desktop terminal shows.

Have a look around. **Esc** backs out of any screen, and **Quit** on the main menu leaves
the app.

Then look at the whole visual language on one card:

```bash
# on the PicoCalc, as meshterm
meshterm specimen
```

There are two console fonts available — a 6×12 one (53 × 26) and a 6×8 one (53 × 40,
more rows but no Cyrillic glyphs). Try both from *Preferences → Display → Console font* —
picking one previews it immediately, and the screen repaints at the new row count so you can
see the choice before you keep it.

---

## Phase 3 — Add the radio

This phase can wait. Everything above already gives you a working MeshTerm in `--mock`
mode. Come back to this when you have the soldering iron out.

### Step 11 — Install PlatformIO

On your PC:

```bash
# on the PC
pip install platformio
```

(or use the
[official installer script](https://docs.platformio.org/en/latest/core/installation/methods/installer-script.html)
if you'd rather not touch your system Python). Confirm `pio` is on your PATH:

```bash
# on the PC
pio --version
```

The first firmware build downloads the nRF52 toolchain — several hundred megabytes — so do
this somewhere with a decent connection.

### Step 12 — Build the firmware

```bash
# on the PC (Git Bash or WSL on Windows)
cd scripts/picocalc/xiao-radio
sh build-firmware.sh
```

You should see it clone MeshCore, apply a patch, build, and finish with:

```
DONE.  Firmware: /path/to/scripts/picocalc/xiao-radio/meshcore-xiao-radio.uf2
Next: put the XIAO in bootloader (double-tap reset) and run:  python flash.py
```

Here's what it did, so you could do it by hand if you needed to:

1. Cloned [MeshCore](https://github.com/meshcore-dev/MeshCore) into
   `scripts/picocalc/xiao-radio/_meshcore-build`.
2. Checked out the pinned commit `e9edfc8e` on the `dev` branch — the commit this patch is
   known to apply cleanly to and build against.
3. Applied `meshcore-uart1.patch`.
4. Built the `Xiao_nrf52_companion_radio_serial` PlatformIO environment:
   `pio run -e Xiao_nrf52_companion_radio_serial`.
5. Converted the resulting `.hex` to a `.uf2` with MeshCore's own converter, using the
   nRF52840 UF2 family id:
   `python bin/uf2conv/uf2conv.py firmware.hex -c -f 0xADA52840 -o meshcore-xiao-radio.uf2`.

#### What the patch changes, and why it is still needed

`meshcore-uart1.patch` touches two files, and both hunks are required:

- **`examples/companion_radio/main.cpp`.** MeshCore's companion-over-serial code declares
  `HardwareSerial companion_serial(1)` — a numbered constructor. On the Adafruit nRF52 core,
  `HardwareSerial` is an abstract base class with no numbered constructor at all, so this
  line simply doesn't compile for the XIAO nRF52840. The patch adds an `#if
  defined(NRF52_PLATFORM)` branch that binds `companion_serial` to `Serial1` instead — the
  concrete UART object the nRF52 core always provides. `Uart::setPins(rx, tx)`, called later
  in the same file, takes the same arguments either way, so nothing else needs to change.
- **`variants/xiao_nrf52/platformio.ini`.** This adds a new
  `Xiao_nrf52_companion_radio_serial` build environment that puts the companion serial link
  on pins **D6 (TX)** and **D7 (RX)**. It also **moves I²C off those same two pads**, onto
  two internal pins, 16 and 17, that nothing in this build uses. This second change is the one that matters: the
  stock `Xiao_nrf52` environment maps I²C onto D6/D7 by default, and both the board's
  `begin()` and the sensor code start the I²C bus on them automatically. Without the move,
  I²C seizes the UART pins at boot — the wiring is correct, the firmware builds and runs,
  and the serial link is still completely silent, because something else already owns the
  pins.

Both changes have been submitted upstream to MeshCore, but as of this writing they are
**not merged**, and there's no sign that will happen soon — upstream `dev` still declares
the bare `HardwareSerial(1)` with no nRF52 branch, and the XIAO's `platformio.ini` still has
no `_serial` companion environment. Building with the patch is the normal route here, not a
stopgap; when the patch does land, the `git apply` step (and the patch file itself) can be
deleted.

#### Building by hand

If you want to track newer MeshCore instead of the pinned commit:

```bash
# on the PC
git clone https://github.com/meshcore-dev/MeshCore.git
cd MeshCore
git checkout dev                            # or MESHCORE_COMMIT=dev sh build-firmware.sh
git apply /path/to/scripts/picocalc/xiao-radio/meshcore-uart1.patch
pio run -e Xiao_nrf52_companion_radio_serial
python bin/uf2conv/uf2conv.py .pio/build/Xiao_nrf52_companion_radio_serial/firmware.hex \
    -c -f 0xADA52840 -o meshcore-xiao-radio.uf2
```

If `git apply` fails — upstream has moved and the patch context no longer lines up — the two
hunks are small enough to make by hand:

1. In `examples/companion_radio/main.cpp`, find the line
   `HardwareSerial companion_serial(1);` and replace it with:

   ```cpp
   #if defined(NRF52_PLATFORM)
     Uart& companion_serial = Serial1;
   #else
     HardwareSerial companion_serial(1);
   #endif
   ```

2. At the end of `variants/xiao_nrf52/platformio.ini`, add a new environment:

   ```ini
   [env:Xiao_nrf52_companion_radio_serial]
   extends = Xiao_nrf52
   board_build.ldscript = boards/nrf52840_s140_v7_extrafs.ld
   board_upload.maximum_size = 708608
   build_flags =
     ${Xiao_nrf52.build_flags}
     -I examples/companion_radio/ui-orig
     -D MAX_CONTACTS=350
     -D MAX_GROUP_CHANNELS=40
     -D OFFLINE_QUEUE_SIZE=256
     -D QSPIFLASH=1
     -D SERIAL_TX=D6
     -D SERIAL_RX=D7
     -D PIN_WIRE_SCL=16
     -D PIN_WIRE_SDA=17
   build_unflags =
     -D PIN_WIRE_SCL=D6
     -D PIN_WIRE_SDA=D7
   build_src_filter = ${Xiao_nrf52.build_src_filter}
     +<../examples/companion_radio/*.cpp>
     +<../examples/companion_radio/ui-orig/*.cpp>
   lib_deps =
     ${Xiao_nrf52.lib_deps}
     densaugeo/base64 @ ~1.4.0
   ```

Only the pinned commit (`e9edfc8e`) is known to build cleanly with this patch — a newer
`dev` may need small further adjustments.

### Step 13 — Flash the XIAO

Plug the **XIAO's own USB-C port** into your PC — it doesn't matter yet whether it's also
wired to the Lyra. Then:

```bash
# on the PC
python flash.py
```

It first tries a 1200-baud "touch" over USB serial to reset the XIAO into its bootloader —
this only ever targets Seeed's own USB vendor id, so it won't disturb another nRF52840 board
on the same bench. If that doesn't take, **double-tap the XIAO's reset button** and run it
again.

Once it finds the bootloader, it checks two things before writing anything:

- **`INFO_UF2.TXT`'s `Model` field** must say a XIAO — a drive letter isn't an identity, and
  another board could have inherited the letter your XIAO just gave up.
- **`SoftDevice` must not be `6.1.1`.** This firmware links for SoftDevice S140 v7 (app at
  `0x27000`); a S140 6.1.1 bootloader wants the app at `0x26000` and would flash successfully
  and then simply never boot.

`--force` overrides both checks, if you're certain. A clean flash ends with:

```
DONE. XIAO flashed and rebooting into the radio firmware.
```

**If no UF2 drive ever appears**, that's not necessarily wrong — some XIAO bootloaders
expose only a serial port, no mass-storage drive at all. `flash.py` recognises this case and
prints the fallback command instead of failing silently:

```bash
# on the PC, from your MeshCore checkout
pio run -e Xiao_nrf52_companion_radio_serial -t upload --upload-port <bootloader port>
```

That command ends in `Device programmed.` and the XIAO reboots itself into the radio
firmware.

Afterwards, the XIAO's USB serial port goes **silent** to companion frames — the companion
protocol now lives on D6/D7, not on USB. That silence is correct; it doesn't mean the flash
failed.

### Step 14 — Wire the radio

Four wires, TX and RX crossed:

| XIAO pad | → | Lyra header | Physical pin | Carries |
| --- | --- | --- | --- | --- |
| **D7** (Serial1 RX) | → | **GP4** | pin **6** | Lyra UART1 **TX** → XIAO RX |
| **D6** (Serial1 TX) | → | **GP5** | pin **7** | XIAO **TX** → Lyra UART1 RX |
| **GND** | → | **GND** | pin **8** | common ground |
| **3V3** | → | **3V3 OUT** | pin **36** | power, so it runs without USB |

> **The Lyra does not use Raspberry Pi Pico GPIO numbering.** The 40-pin header is
> physically Pico-shaped, but each pad's function is Luckfox's own RK3506 mapping: GP4 is
> `gpio0-0` and GP5 is `gpio0-1`. Don't reach for a Pico pinout for anything else on this
> header. Every signal here is 3.3 V, and there is **no 5 V rail** on this board — power the
> XIAO from 3V3 only.

With the PicoCalc powered off and the back of the shell removed, solder the four wires
between the XIAO's pads and the Lyra's header pins as above. The Lyra can stay seated in
its socket: the tops of its header pins are reachable from inside the case, so solder to
those. Keep the iron on each pin only as long as it takes, and don't press down on the
board. Attach the LoRa antenna to the Wio-SX1262's u.FL connector before you
power anything back on — running the radio without an antenna attached can damage it.
Wrap the XIAO + Wio-SX1262 stack in Kapton tape so no pad can touch the Lyra, the
mainboard, or a battery, then tuck it into the shell wherever it fits without straining the
wires; a strip of foam tape holds it in place against the inside of the case.

### Step 15 — Set up the Lyra

With the wiring done and the shell closed back up, copy `scripts/` to the device if you
haven't already (Step 8), then:

```bash
# on the PicoCalc, as root
sh scripts/picocalc/xiao-radio/lyra-setup.sh          # MT_USER=meshterm by default
```

It does three idempotent things:

1. Installs `uart1-mux.py` as `/usr/local/bin/uart1-radio-mux.py`, run by a oneshot
   `uart1-radio-mux.service` on every boot. This is the part that actually makes
   `/dev/ttyS1` reach the header pins — Calculinux enables the UART1 controller but never
   wires it to a physical pad on its own, and the RK3506's flexible pin matrix needs a
   direct register poke to route it onto GP4/GP5 instead.
2. Adds `$MT_USER` to the `dialout` group, so it can open the serial port. **This needs a
   fresh login before it takes effect.**
3. Writes a MeshTerm profile, *only if none exists yet*, at
   `/home/$MT_USER/.meshterm/config.toml`:

```toml
default_profile = "picocalc"

[profiles.picocalc]
port = "/dev/ttyS1"
baudrate = 115200
transport = "serial"
default_tx_power = 22
description = "XIAO nRF52840 + SX1262 via Lyra UART1 on GP4/GP5"
```

If a `config.toml` already exists, it leaves your file untouched and prints only the
`default_profile` line, the `[profiles.picocalc]` heading and the `port`. Copy the whole
block above into your file yourself.

### Step 16 — Run it

Log out and back in as `meshterm` (so the `dialout` group membership takes effect), then:

```bash
# on the PicoCalc, as meshterm
meshterm
```

A bare `meshterm` opens on the **Select a companion device** splash, with the `picocalc`
profile listed as a row; pick it. To skip the splash, run `meshterm -p picocalc`. You
should see your node come up under whatever name the firmware advertises — both the device
page and the dashboard header show it.

If you want to prove the wiring itself before suspecting anything else, there's a deeper
check: open `/dev/ttyS1` at 115200 as root and send a raw `APP_START` frame (`3c 0e 00 01`,
seven zero bytes, then a name) — a live radio answers with a framed `05`, `SELF_INFO`. The
command byte and the reply code are the companion protocol's; the `<`/`>` and
little-endian-length framing around them come from the firmware and are worth confirming
against it. It's a lower-level check than running MeshTerm and mostly useful for isolating
a wiring problem.

---

## Day-to-day

**Charging.** Only through the mainboard's USB-C port on the side of the case. ⚠ Never
through the Lyra's own USB-C at the back — see
[Three ways to break it](#three-ways-to-break-it).

**Powering off.** Shut down cleanly rather than pulling the batteries:

```bash
# on the PicoCalc
sudo poweroff
```

**Updating MeshTerm.** The copy of `scripts/` you made in Step 8 goes stale as soon as
the checkout moves on, so run the scripts inside the checkout instead. Either pull and
re-run the setup script from root (it reinstalls and rebuilds the console font):

```bash
# on the PicoCalc, as root
su - meshterm -c 'git -C ~/MeshTerm pull'
sh /home/meshterm/MeshTerm/scripts/picocalc/calculinux-setup.sh
```

or, as the `meshterm` user, do just the update:

```bash
# on the PicoCalc, as meshterm
cd ~/MeshTerm && git pull && TMPDIR=$HOME/tmp .venv/bin/pip install -e .
```

If an update adds new glyphs to the console font, rebuild it on its own afterwards:

```bash
# on the PicoCalc, as root
sh /home/meshterm/MeshTerm/scripts/picocalc/calculinux-console-font-6x12.sh
```

**Changing Wi-Fi.** Run `uwific` as root, as in [Step 7](#step-7--join-wi-fi). The boot-time
kick picks up the new network on its own; it only needs `iwd` to know it.

**The slow boot without a dongle.** If the device is going to run permanently offline,
the Wi-Fi kick and the time sync are wasted time on every boot. Disabling the kick is not
enough, because the time sync pulls it back in, so mask it. Then disable the time sync,
which would otherwise still wait up to five minutes for a network:

```bash
# on the PicoCalc, as root
systemctl mask --now wifi-kick.service
systemctl disable --now time-sync.service
```

To undo it later, `systemctl unmask wifi-kick.service` and
`systemctl enable time-sync.service`.

**The console font restores itself.** MeshTerm saves whatever font the console was using
before it starts, and puts it back when it exits — on a normal quit and on a crash alike.
You don't need to do anything to keep the shell's own font intact.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Nothing on the PicoCalc's screen at all | Is the SD card in the **Lyra's own slot**, not the PicoCalc's front slot? Is a "Lyra B" (SPI NAND) erased? Is it the **128 MB RAM, Pico-form-factor** variant — other RAM sizes have incompatible pinouts? |
| Wi-Fi never comes up | Is the dongle one of the tested chipsets (RTL8192CU / R8712U / RTL8188EU)? Remember the Lyra's USB port is **3.3 V** — a 5 V dongle won't work. Give it the full six minutes on a cold boot before assuming it's stuck. |
| `pip install` fails with `ENOSPC` | `/tmp` is a small RAM disk. `calculinux-setup.sh` already sets `TMPDIR=$HOME/tmp` for its own install; if you're running `pip` by hand, do the same. |
| `meshterm: command not found` after setup | Log out and back in — the PATH line is added to `~/.profile`, which only takes effect on a fresh login shell. |
| Tofu boxes instead of braille charts or node glyphs | The console font script hasn't run, or didn't persist. Re-run `sh calculinux-console-font-6x12.sh`, and check `/etc/vconsole.conf` has a `FONT=` line. |
| `meshterm` can't open `/dev/ttyS1` | Is the `meshterm` user in `dialout` (`groups`)? Group changes need a fresh login to take effect. |
| Port opens but the radio never answers | Re-flash with the **`_serial`** environment, not `_ble` or `_usb` — only it defines `SERIAL_RX`/`SERIAL_TX`. The XIAO's USB serial should be silent once it's running the radio firmware; that's expected, not a fault. |
| Still silent with wiring confirmed | Confirm the firmware actually carries the I²C remap (`PIN_WIRE_SCL=16`, `PIN_WIRE_SDA=17`) — a build without the patch's second hunk looks identical until you check this. |
| Flashed fine, XIAO never comes back | Wrong board, or the wrong SoftDevice — `INFO_UF2.TXT` must report a XIAO and **S140 v7**, not `6.1.1`. `flash.py` refuses to flash either mismatch without `--force`. |
| `flash.py` finds no UF2 drive | Expected on a CDC-only bootloader — use the `pio … -t upload --upload-port` command it prints instead. |
| Nothing on `/dev/ttyS1` after a reboot | `systemctl status uart1-radio-mux`, then `journalctl -b -u uart1-radio-mux` — the pin-mux poke has to run every boot, and a failed oneshot says why. |

---

## What has been tested

This whole path — from a stock PicoCalc kit through a running radio — has been exercised on
one maintainer's bench: one Luckfox Lyra, one PicoCalc, one XIAO + Wio-SX1262 pair. It
works. One part is also checked in code: a test (`tests/test_theme16.py`) parses the font
script and pins it to MeshTerm's glyph inventory. The pinned MeshCore commit, the need for
the patch's two hunks on `dev`, and the nRF52840 UF2 family id and XIAO bootloader ids
against Adafruit's board files were checked by hand when this was written.

The rest is one bench's findings, and a second device may disagree:

- **The RK3506 register map** in `uart1-mux.py` — the two register blocks, the UART1 signal
  ids, matrix mode, and that `gpio0-0`/`gpio0-1` are header GP4 and GP5 — was probed live
  on one board, not read from a reference manual.
- **That the Calculinux kernel permits those `/dev/mem` writes**, and that the mux oneshot
  really runs at every boot.
- **The header pin numbers** 6, 7, 8, and 36 for GP4, GP5, GND, and 3V3. The Pico-side
  numbering is right; the carrier's wiring is a bench observation.
- **The MX1.25 socket's pin order.** Taken from the wire colours of Luckfox's own cable,
  which is why [Step 3](#step-3--attach-the-wi-fi-dongle) has you check pin 1 with a meter.
- **The 5 V path through the Lyra's USB-C.** The mechanism is read off the mainboard
  schematic and two forum measurements on the Pico; nobody has measured it with a Lyra.
  Treat the warning as the safe assumption it is.
- **The Calculinux specifics the scripts assert**: the stripped Python and the package
  names that fix it, which overlays are writable, that `/tmp` is a small RAM tmpfs, the
  dongle's cold-boot race with `iwd`, the opkg feed's occasional 404, that the stock
  Terminus console font is installed, and that `kbd` brings `setvtrgb`.
- **That this board has no RTC.** Only the script says so; the clock-sync phase follows.
- **The XIAO bootloader that exposed no mass storage**, and the S140 v7 / `0x27000`
  requirement — public references agree, but neither is a primary source.
- **Whether the firmware patch still applies at MeshCore `dev` HEAD.** Only the pinned
  commit is known to build.
- **MeshCore's serial framing** around the raw `APP_START` check in
  [Step 16](#step-16--run-it): the command byte and reply code are from the protocol
  documentation; the framing bytes are not.
- **The framebuffer console's 512-glyph cap.** Well known, taken as given, not re-measured.

If you build this and something disagrees with what's written here, that's worth
reporting — a second board is the only way to learn which parts of this were about the
hardware and which were about the one unit it was built on.
