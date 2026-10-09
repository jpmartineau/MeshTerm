# MeshTerm on the PicoCalc

You start with a stock ClockworkPi PicoCalc (the kit that comes with a Raspberry Pi Pico)
and a short shopping list. You end with a Linux handheld. MeshTerm runs full-screen on the
display and the keyboard of the PicoCalc. A LoRa radio is soldered inside. You do not need
a laptop for daily use.

There are three phases:

1. Build the machine. You replace the Pico with a Linux board.
2. Install the OS and MeshTerm.
3. Add the radio.

Plan an afternoon for the first two phases. Most of this time is the wait for a toolchain
to download. The radio phase needs a soldering iron, and you can do it later. In the
meantime, the `--mock` mode of MeshTerm runs well without a radio.

**Do you want to see MeshTerm before you build the machine?** On any computer that has
MeshTerm installed, run `meshterm emulate picocalc-lyra --mock`. It opens a window that
shows MeshTerm as the display of the PicoCalc draws it, with a simulated radio. The
emulator is not a strict emulator. It copies the size, the font, and the colours of the
display, but it does not copy the machine. It shows you what you will build.
[The emulator](README.md#the-emulator) has the details.

> ⚠ **WARNING: Read this first.** In this build, you take the PicoCalc apart, replace its
> core board, and solder to it. You must be able to solder cleanly, use a multimeter, and
> work with small electronics without force. If a step goes wrong, the hardware can be
> damaged: a cracked screen, a broken shell post, a dead board, or an overcharged lithium
> cell. Some of this damage cannot be repaired. You do this at your own risk.
>
> **Read the whole manual one time before you buy or open anything.** Several steps are
> clear only when you know a later step. These are where the SD card goes, what you must
> remove from the case before you press in a board, and which USB port you must never
> power. You can also make the shopping list more easily when you know what each part is
> for.
>
> This is a guide, not a prescription. The author tried to make it correct, but it can
> have errors. Parts of it will become out of date when boards, images, and firmware
> change. You must check each fact in it against your own hardware and the current sources
> before you act on it. Each decision is yours. The author accepts no responsibility for
> damage, loss, or injury that results from this guide. It is not a warranty that your
> parts, your tools, or your hands will work as the parts, tools, and hands of the author
> did. If you are not comfortable with a step, stop and ask for help from a person who is.

- [Shopping list](#shopping-list)
- [Before you start](#before-you-start)
- [Three ways to break it](#three-ways-to-break-it)
- [Phase 1: Build the machine](#phase-1-build-the-machine)
  - [Step 1: Assemble the PicoCalc](#step-1-assemble-the-picocalc)
  - [Step 2: Swap the Pico for the Lyra](#step-2-swap-the-pico-for-the-lyra)
  - [Step 3: Attach the Wi-Fi dongle](#step-3-attach-the-wi-fi-dongle)
- [Phase 2: Install Calculinux and MeshTerm](#phase-2-install-calculinux-and-meshterm)
  - [Step 4: Write the image to the microSD](#step-4-write-the-image-to-the-microsd)
  - [Step 5: First boot](#step-5-first-boot)
  - [Step 6: Open the serial console](#step-6-open-the-serial-console)
  - [Step 7: Join Wi-Fi](#step-7-join-wi-fi)
  - [Step 8: Copy the setup scripts to the device](#step-8-copy-the-setup-scripts-to-the-device)
  - [Step 9: Run calculinux-setup.sh](#step-9-run-calculinux-setupsh)
  - [Step 10: Log in as meshterm](#step-10-log-in-as-meshterm)
- [Phase 3: Add the radio](#phase-3-add-the-radio)
  - [Step 11: Install PlatformIO](#step-11-install-platformio)
  - [Step 12: Build the firmware](#step-12-build-the-firmware)
  - [Step 13: Flash the XIAO](#step-13-flash-the-xiao)
  - [Step 14: Wire the radio](#step-14-wire-the-radio)
  - [Step 15: Set up the Lyra](#step-15-set-up-the-lyra)
  - [Step 16: Run it](#step-16-run-it)
- [Day-to-day](#day-to-day)
- [Troubleshooting](#troubleshooting)
- [What has been tested](#what-has-been-tested)

---

## Shopping list

### The machine

| Item | Product / what to search for | Price (approximate) | Link | Note |
| --- | --- | --- | --- | --- |
| PicoCalc kit | ClockworkPi PicoCalc | $89 | [clockworkpi.com/product-page/picocalc](https://www.clockworkpi.com/product-page/picocalc) | The kit has a Raspberry Pi Pico 1H, a screen, a keyboard, a shell, a hex key, and a 32 GB SD card. **It does not have 18650 batteries.** |
| Luckfox Lyra | "Luckfox Lyra 128MB, pre-soldered header, no NAND" | $20–25 | [luckfox.com/Luckfox-Lyra](https://www.luckfox.com/Luckfox-Lyra) | Buy the **plain 128 MB RAM, Pico-form-factor** variant, **with the header pre-soldered**. Other RAM sizes have pinouts that do not fit. This is the one part of the list where the wrong SKU does not work. |
| microSD card | 16 GB+, Class 10 or better | $6–10 | none | The stock 32 GB card of the PicoCalc works. It is more than the minimum of 8 GB. You can use it again and not buy a new card. |
| An 18650 battery | For example Samsung 30Q, Molicel P26A, Sony VTC6 (examples, not endorsements) | $5–10 | none | **Unprotected**, flat-top or button-top, Ø18 × 65–69 mm. One battery is enough to run the machine. A second battery is optional (refer to [the battery note](#a-note-on-batteries) below). |
| USB Wi-Fi dongle | TP-Link TL-WN725N (an RTL8188EU nano dongle). You can also search for the **chipset name**: RTL8192CU, R8712U, RTL8188EU | $5–10 | [amazon.ca/dp/B008IFXQFU](https://www.amazon.ca/dp/B008IFXQFU) | The TL-WN725N is the dongle that this guide used. It must run at **3.3 V** (refer to [Step 3](#step-3-attach-the-wi-fi-dongle)). You need it only for setup and updates. MeshTerm itself runs offline. |
| MX1.25 4-pin pigtail | "MX1.25 4P cable with leads" (a pack of pre-crimped pigtails, 10–15 cm) | $2–4 | none | One end of the DIY USB lead in [Step 3](#step-3-attach-the-wi-fi-dongle). Buy it pre-crimped, because MX1.25 contacts need a crimper that you probably do not have. |
| USB-A female socket | "USB 2.0 type A female socket, solder / through-hole" | $2–3 | none | The other end of the DIY lead. A ready-made [MX1.25 4P to USB-A cable](https://spotpear.com/shop/Luckfox-Lyra-MX1.25-4P-To-USB-A-Cable.html) (approximately $1, 30 cm) also works. But it is much cable to fold into the shell. |

### The radio

| Item | Product / what to search for | Price (approximate) | Link | Note |
| --- | --- | --- | --- | --- |
| XIAO nRF52840 + Wio-SX1262 kit | "XIAO nRF52840 & Wio-SX1262 Kit for Meshtastic", SKU 102010710 | $13.49 | [seeedstudio.com](https://www.seeedstudio.com/XIAO-nRF52840-Wio-SX1262-Kit-for-Meshtastic-p-6400.html) | The kit has the XIAO nRF52840, the Wio-SX1262 LoRa board (stacked and wired together), and an antenna. It covers 868 MHz and 915 MHz. The plain XIAO and the Sense XIAO both work. **Do not** buy the "-N" variant. It has no antenna connector. |

### Tools and consumables

| Item | Note |
| --- | --- |
| Soldering iron, fine tip, and solder | For the four radio wires. |
| Hookup wire, 26–30 AWG, four short lengths | TX and RX are crossed (refer to [Step 14](#step-14-wire-the-radio)). |
| 2.5 mm hex key | It comes with the PicoCalc kit. |
| USB-C data cable | For the serial console, and later to flash the XIAO. A cable that only charges does not work. The cable must carry data. |
| microSD card reader | To write the Calculinux image from your PC. |
| Small side cutters | To cut wire ends. Also to cut a section of the case grille, if the Wi-Fi lead needs the space. |
| Small needle file | To make the USB-C cutout in the back panel a little bigger, if the connector of the Lyra does not clear it. |
| Kapton (polyimide) tape | It wraps the XIAO stack and the DIY USB lead. Then no bare part touches the board or the shell. It is thin, it resists heat, and it comes off cleanly. |
| A PC (Windows/macOS/Linux) with Python 3 and git | It runs the steps that write the image, build the firmware, and flash the XIAO. |

### Optional

| Item | Why |
| --- | --- |
| A second 18650 battery | The holder takes two batteries. One is enough to run the machine. Two give approximately twice the runtime. |
| Powered USB hub | Use it if you want the Wi-Fi dongle and another device on the single USB host socket of the Lyra at the same time. Also use it for a 5 V dongle. |
| Foam tape | To hold the XIAO board in the shell after you wire it. |

The approximate total is **$130–150** for the machine and the radio together, before
batteries. Most of this is the PicoCalc kit ($89), the Lyra (approximately $22), and the
radio kit ($13.49).

**What you do NOT need:**

- A level shifter. Each signal in this build is 3.3 V: the header of the Lyra, the XIAO,
  and the SX1262.
- A separate USB-to-serial adapter. The PicoCalc mainboard has one built in.
- Bluetooth. Neither the Lyra nor this radio path uses it.
- A new SD card, if you have the stock 32 GB card. It is much more than the minimum of
  8 GB that Calculinux asks for.

### A note on batteries

ClockworkPi did not publish an official battery specification for the PicoCalc. A
[GitHub issue](https://github.com/clockworkpi/PicoCalc/issues/6) that asks for one is
still open and has no answer. These facts come from the kit itself:

- The holder takes two 18650 cells.
- One cell is enough to power the board.
- The physical size is approximately Ø18 mm × 65–69 mm. This excludes most *protected*
  cells, because the protection circuit adds length that does not fit.
- Unprotected flat-top cells and button-top cells both work.
- The PicoCalc mainboard has its own charge circuit.

Buy from a reputable brand, not from a cheap unbranded listing. Samsung 30Q, Molicel P26A,
and Sony VTC6 are examples that people recommend often. They are not an endorsement of one
of them.

---

## Before you start

You work on two machines. It helps to know which machine does which job:

- **Your PC** writes the Calculinux image to the microSD card, builds the radio firmware,
  and flashes the XIAO. These three jobs do not touch the PicoCalc itself.
- **The PicoCalc** (the Luckfox Lyra inside it) does all other jobs: setup, Wi-Fi, and
  MeshTerm. You can do these jobs over the serial console from your PC, or on the display
  and the keyboard of the PicoCalc. Both ways work in the same manner. The serial console
  is easier when you must paste long commands.

The PicoCalc has two logins for different jobs:

- **`root`** is for the setup script and for Wi-Fi repairs. You use it one time.
- **`meshterm`** is the unprivileged account that MeshTerm runs under each day.

Calculinux is a Linux that has a console only. It has no desktop and no window manager.
This is intentional. The display and the keyboard of the PicoCalc are a terminal. MeshTerm
is a full-screen terminal program that is made for this.

---

## Three ways to break it

The PicoCalc is a kit. Its forum has a long list of the ways in which people damaged their
PicoCalc. Three of them apply to this build. The first two are real. The author of this
guide **cracked a screen and broke screw posts** when building the machine that this guide
describes. Read this section before you open the shell.

> ⚠ **WARNING: The screen is very fragile.** It is a bare glass panel with no frame. When
> the case is closed, the screen is directly against the mainboard. Each force that bends
> the mainboard also bends the glass. These are the actions that bend it:
>
> - You push a board into the Pico socket.
> - You pull a board out.
> - You close the shell when the screen is not square in its opening.
> - You press a key while you screw the two halves together.
>
> Forum members cracked screens in each of these ways. The swap in
> [Step 2](#step-2-swap-the-pico-for-the-lyra) is the classic case. If you press a new
> board into the socket while the mainboard is in the case, this alone can crack the
> screen. This is how the screen of the PicoCalc of the author broke.
> ([Before replacing the Pico](https://forum.clockworkpi.com/t/before-replacing-the-pico-read-this-to-avoid-cracked-screen/16666),
> [Avoid breaking your screen](https://forum.clockworkpi.com/t/avoid-breaking-your-screen/18805))
>
> Do these things:
>
> - **Never push a board into the socket, or pull one out, while the mainboard is in the
>   case.** Unplug the ribbon cable of the display (the FPC) from the mainboard. Lift the
>   mainboard out. Do the swap on a flat, padded surface. A folded microfibre cloth is
>   good. Ease a board out a little at each end in turn. Never lever one end.
> - **Hold the screen with Kapton tape** along its edges. Then it cannot move in the
>   opening while you work or while the case is closed. Kapton is the correct tape. It is
>   thin, it stays in position, and it comes off cleanly. Do not use a tape that sets
>   hard.
> - **Before you fit the back, check that the screen is flush and square** in the opening
>   of the front half, with no pressure on a corner. If the mainboard is not perfectly
>   flush on the front half, stop. Find the cause before you add the back.
> - Touch the panel as little as possible. Keep your fingers off the keyboard while you
>   close the shell.

> ⚠ **WARNING: Do not tighten the screws too much.** The screw posts of the shell are
> brass inserts that are moulded into plastic. If you force a screw, the post breaks out of
> the shell. This repair needs epoxy and is never as good as the original
> ([Broken screw thread](https://forum.clockworkpi.com/t/broken-screw-thread/20726)).
> The author broke several posts at the first assembly. You need less force than you think.
> Turn each screw until it just seats, then stop. If a screw does not go in easily, the two
> halves are not aligned, or a part inside is in the way. More force is never the answer.

> ⚠ **WARNING: Never power the Lyra through its own USB-C while the batteries are in.**
> The PicoCalc charges its cells only through the USB-C port of the **mainboard**. A
> power-management chip (an AXP2101) controls the charge. Power that comes in through the
> USB port of the core board (the Pico's originally, now the Lyra's) goes to the
> system-power pin of the header. It bypasses the charge control. Forum members who powered
> the port of the Pico directly measured **4.46 V and 4.74 V** on their cells afterwards.
> This is much more than the 4.2 V that a lithium cell can have
> (["New PicoCalc — battery charging"](https://forum.clockworkpi.com/t/new-picocalc-battery-charging/22330)).
> An overcharged cell is a fire risk, not only a worn cell.
>
> Nothing in this guide needs the USB-C of the Lyra. The serial console, the charging, and
> the power all use the port of the mainboard on the side of the case. The Wi-Fi dongle uses
> the 4-pin socket on top of the Lyra. If you must connect the USB-C of the Lyra to a PC
> (for example, to flash it again with the tools of Luckfox), **remove both batteries
> first.**

---

## Phase 1: Build the machine

### Step 1: Assemble the PicoCalc

1. Follow the
   [assembly guidelines](https://github.com/clockworkpi/PicoCalc/blob/master/Clockwork_PicoCalc_Assembly_Guidelines.pdf)
   of ClockworkPi to put the kit together. The guidelines are a PDF in the
   [PicoCalc repository](https://github.com/clockworkpi/PicoCalc), which also has a wiki.
   You assemble the mainboard, the screen, the keyboard, the speakers, and the shell.
2. Put in your 18650 battery (or two). Look at the `+`/`-` marks in the battery
   compartment.

WARNING: Two of the warnings in [Three ways to break it](#three-ways-to-break-it) apply
here. Before you close the shell, tape the edges of the screen with Kapton and check that
the screen is square. Turn the screws only until they seat. You open the shell again in
the next step. Thus there is no reason to make the screws tight now.

3. Turn the power on one time with the **stock Pico still in place**. You must see the
   stock BASIC firmware start on the screen. The keyboard must respond. This confirms that
   the screen, the keyboard, and the battery are good before you replace boards.
4. Turn the power off.

### Step 2: Swap the Pico for the Lyra

1. Remove the back of the shell with the hex key.

WARNING: Do the swap with the mainboard out of the case. This step is the one that cracks
screens (refer to [Three ways to break it](#three-ways-to-break-it)).

2. Unplug the ribbon cable of the display from the mainboard.
3. Lift the mainboard out and put it on a padded surface.
4. Ease the Raspberry Pi Pico out of its socket, a little at each end in turn. **Keep the
   Pico.** You only put it to one side.
5. Check if your Luckfox Lyra has SPI NAND on the board (a "Lyra B"). If it has, erase the
   NAND first. If you do not erase it, the boot ROM ignores the SD card and starts from
   the flash on the board. Refer to
   [the hardware requirements page of Calculinux](https://calculinux.org/getting-started/hardware-requirements/)
   to see how.
6. Put the Lyra in the same socket that the Pico left. Keep the same orientation. The USB-C
   port of the Lyra must be at the end where the USB port of the Pico was. Then it lines up
   with the cutout in the back of the shell.
7. Press the Lyra fully down while the mainboard is still on the padded surface. A Lyra
   that is not fully seated does not start.
8. Connect the ribbon cable again.
9. Put the mainboard back in the front half. Check that the screen is square before you
   tighten any screw.

You must see the Lyra flat on the socket, with no gap at either end. After the mainboard is
back in the case, the USB-C port of the Lyra must be at the centre of the cutout in the
back panel.

The cutout was made for the micro-USB port of the Pico. The USB-C connector of the Lyra has
a different shape. Depending on the connector that Luckfox fitted to your board, the back
panel may not close over it. If the panel catches, do these steps:

1. Open the hole a little with a small needle file.
2. Test the fit as you file.
3. Stop when the panel sits down without pressure on the board.

Leave the back off for now. In [Step 4](#step-4-write-the-image-to-the-microsd), the
microSD card goes into the **slot of the Lyra itself**. You can reach that slot more easily
when the board is open.

### Step 3: Attach the Wi-Fi dongle

The Lyra has two USB connectors. The USB-C connector on its edge is for power and flashing.
You do not use it here. The **small 4-pin socket on top of the board** is the USB host. It
is an MX1.25 connector, next to the two buttons at the USB-C end. The Wi-Fi dongle connects
to it through a short lead that you make yourself. A ready-made cable is 30 cm long and is
difficult to fold into the shell. Also, you already have the soldering iron out for the
radio.

**Make the lead.** Solder the four wires of an MX1.25 pigtail to a USB-A female socket.
Keep the lead short. A length of 5 to 8 cm is enough to reach a place where the dongle sits
flat. The pins of the pigtail are in the standard USB order. The wire colours of the
ready-made cable follow this order:

| MX1.25 pin | USB-A socket pin | Signal | Pigtail wire colour (usually) |
| --- | --- | --- | --- |
| 1 | 1 | VBUS (**3.3 V** on this board) | red |
| 2 | 2 | D− | white |
| 3 | 3 | D+ | green |
| 4 | 4 | GND | black |

The pins of the USB-A socket are numbered 1 to 4 across the opening of the connector. VBUS
is on one edge, GND is on the other edge, and the data pair is between them. The colours of
a pigtail are a convention, not a guarantee. Thus check pin 1 before you connect anything.

WARNING: Make sure that the soldering iron is in its stand when you do not use it. The tip
is hot enough to burn your skin.

CAUTION: Do not swap VBUS and GND. This is the one mistake that damages the dongle.

1. Solder the four wires to the USB-A socket.
2. Turn the power of the Lyra on.
3. With a multimeter, measure pin 1 of the socket against a GND pin of the header. It must
   read **3.3 V**.
4. Measure the pin at the other end of the socket. It must read 0 V.
5. Turn the power off.
6. Wrap the solder side of the socket and the bare wire in Kapton tape.
7. Push the MX1.25 end into the socket on the Lyra. The socket is keyed, and the plug goes
   in only one way.
8. Push the Wi-Fi dongle into the USB-A end.
9. Put the lead and the dongle inside the shell, where they do not press on the board.

The MX1.25 socket is on top of the Lyra, and the lead leaves it upward. The grille of the
back panel is there. The wire can interfere with the grille when you fit the back. If it
does, cut a small section out of the grille with the side cutters, where the wire passes.
Shape the edge until nothing presses on the plug or the wire. It is better to remove a
sliver of plastic than to close the case against a connector.

> **CAUTION: The USB port of the Lyra is 3.3 V, not 5 V.** A standard 5 V USB Wi-Fi dongle
> does not work and can damage the board. Use only the tested chipsets: RTL8192CU, R8712U,
> or RTL8188EU. For this reason, the shopping list names a known dongle (the TP-Link
> TL-WN725N). For other dongles, it tells you to search for the chipset and not for the
> brand.

Do not turn the power on yet. You set up Wi-Fi later, after you install Calculinux.

---

## Phase 2: Install Calculinux and MeshTerm

### Step 4: Write the image to the microSD

1. On your PC, download `calculinux-image-luckfox-lyra.rootfs-<timestamp>.wic.gz` from the
   newest release on the
   [Calculinux releases page](https://github.com/Calculinux/meta-calculinux/releases).
   Each Calculinux release is marked **Pre-release**. Thus there is no "Latest" badge to
   follow. Take the newest pre-release.
2. Decompress the file:

```bash
# on the PC
gunzip calculinux-image-luckfox-lyra.rootfs-*.wic.gz
```

   The Calculinux installation page mentions `unxz`, but the file is a `.gz`. Thus use
   `gunzip`. If you do not want to use the command line, 7-Zip works on all platforms.

CAUTION: Write the `.wic` file to the correct device. This action erases everything that is
on the card.

3. Write the resulting `.wic` file to your microSD card. Use one of these three tools:

   - **Balena Etcher** (recommended, all platforms): open it, select the `.wic` file,
     select your SD card, and click Flash.
   - **`dd`**, on Linux or macOS. Check the device name first. If you write to the wrong
     device, you destroy its contents:

```bash
  # on the PC
  lsblk                                        # find your SD card's device, e.g. /dev/sdb
  sudo dd if=calculinux-image-luckfox-lyra.rootfs-*.wic of=/dev/sdX bs=4M status=progress conv=fsync
```

   - **Rufus**, on Windows: select the device and the image, choose the **MBR** partition
     scheme, and start.

4. When the write is complete, eject the card.
5. Put the card in the **Luckfox Lyra's own microSD slot**. Do not use the front slot
   of the PicoCalc. The Lyra starts from its own slot. You can reach the front slot only
   after MeshTerm (or another program) already runs on Linux.
6. Leave the front slot **empty** for the first boot. The instructions of Calculinux also
   say this. A second card there that has the same partition labels makes the system
   read-only.

### Step 5: First boot

1. With the card in the Lyra, turn the PicoCalc on.
2. Wait 30–60 seconds. The first boot is slower than the next boots. Boot messages scroll
   on the display of the PicoCalc. They end with a `Calculinux GNU/Linux` login prompt.
3. Log in:

```
login: root
Password: root
```

4. Change the password at once:

```bash
# on the PicoCalc, as root
passwd
```

### Step 6: Open the serial console

The display and the keyboard of the PicoCalc work well, thus you can skip this step. But
the serial console makes it much easier to paste the longer commands in this guide. We
recommend it.

The USB-C port of the PicoCalc mainboard has a USB-to-UART bridge (a CH340 chip).

1. Connect a USB-C cable from that port to your PC. The port shows as:

   - `/dev/ttyUSB0` on Linux (you may need to be in the `dialout` group)
   - a `COM` port on Windows
   - `/dev/cu.usbserial-*` on macOS

2. Open a terminal program with these settings: **1500000 8N1**, no flow control. This
   example uses `miniterm` of `pyserial`:

```bash
# on the PC
pip install pyserial
python3 -m serial.tools.miniterm /dev/ttyUSB0 1500000
```

minicom and PuTTY also work, with the same settings. To leave `miniterm`, press
**Ctrl+]**.

You must see the same login prompt that you saw on the display of the PicoCalc, now in the
terminal of your PC.

### Step 7: Join Wi-Fi

Calculinux manages Wi-Fi with `iwd`. The easiest way is its text-mode picker.

1. Run `uwific`.
2. Highlight your network and press Enter.
3. Type the passphrase. (`Q` quits.)

You can do the same from the command line:

```bash
# on the PicoCalc, as root
iwctl station wlan0 scan
iwctl station wlan0 get-networks
iwctl station wlan0 connect "<your SSID>"
```

The last command asks for the passphrase. Then check that you are online:

```bash
ip addr show wlan0
ping -c 3 1.1.1.1
```

You must see an `inet` address on `wlan0`, and replies from the ping. `iwd` remembers the
network. Thus you do this step one time. To change networks later, run `uwific` again.

### Step 8: Copy the setup scripts to the device

You need the `scripts/` directory of this repository on the Lyra. The Lyra runs the
`picocalc-lyra/` folder in it. When Wi-Fi is up, you can get the directory there in two
ways:

- **`scp` from your PC.** On the Lyra, run `ip addr show wlan0` to find its IP address.
  Then, on your PC, run:

  ```bash
  # on the PC, from your MeshTerm checkout
  scp -r scripts root@<lyra-ip>:/root/
  ```

  If SSH refuses root, copy as the `pico` user (password `calc`). Then move the directory
  as root on the Lyra:

  ```bash
  # on the PC
  scp -r scripts pico@<lyra-ip>:/home/pico/
  # on the PicoCalc, as root
  mv /home/pico/scripts /root/
  ```

- **`git clone` on the Lyra.** The Calculinux base image includes `git`. As root, run
  `git clone https://github.com/jpmartineau/MeshTerm`. Then use the `scripts/` folder in
  the clone.

We recommend `scp`.

### Step 9: Run calculinux-setup.sh

```bash
# on the PicoCalc, as root, with scripts/ copied over
cd scripts/picocalc-lyra
sh calculinux-setup.sh
```

You can run the script again without a problem, and it checks before it acts. It reads
these environment variables at the top of the script:

| Knob | Default | Effect |
| --- | --- | --- |
| `DEPLOY_USER` | `meshterm` | The login that MeshTerm runs under |
| `TIMEZONE` | `America/Toronto` | Any IANA zone. Set it if you are not on Eastern time. |
| `REPO_URL` | the HTTPS URL of the project | The script clones this URL when there is no deploy key. |
| `REPO_SSH` | the SSH URL of the project | The script clones this URL when there is a deploy key. |
| `KEY_PATH` | `/home/$DEPLOY_USER/.ssh/id_ed25519` | A read-only GitHub deploy key, if you want SSH and not HTTPS |
| `WIFI_SSID`, `WIFI_PSK` | unset | If you set both, the script writes the credentials of `iwd` for that network. This is an alternative to Step 7. |

The defaults clone the public repository of MeshTerm over plain HTTPS. This needs no
credential. You probably need only `TIMEZONE`, if you live outside Eastern time:

```bash
# on the PicoCalc, as root
TIMEZONE=America/Vancouver sh calculinux-setup.sh
```

The script installs only the time-zone data for the Americas. Thus a zone in another region
may not work. If the script cannot set the zone, it prints a warning and the clock stays on
UTC.

The script does these things, one for each phase:

- **opkg packages.** It installs `python3-modules` and `python3-pip`. It also installs
  `git` and `kbd` if the image does not have them. Calculinux has a reduced Python that
  does not have `pip`, `venv`, and several modules of the standard library.
- **Wi-Fi kick.** It installs a service for boot time that scans again until the dongle
  joins the network that `iwd` already knows. On a cold boot, the firmware of the dongle
  finishes loading after the first scan of `iwd`. Without the kick, the link does not come
  up.
- **Time sync.** It installs a service for boot time that sets the clock over the network,
  because this board has no battery-backed real-time clock.
- **Timezone.** It sets `$TIMEZONE` (`America/Toronto` by default).
- **Deploy user.** It creates the `meshterm` login and puts it in the groups that it needs.
- **Clone.** It checks out MeshTerm to `/home/meshterm/MeshTerm`.
- **venv and install.** It builds a Python virtual environment and runs `pip install -e .`
  to install MeshTerm in it.
- **Login PATH.** It puts the `bin` of that venv on the PATH of the `meshterm` user.
- **Console font.** It builds and installs the console font that the UI of MeshTerm needs
  (braille charts, node glyphs, and rounded corners).

This takes a few minutes on a good connection. Most of the time is `pip install`, which
compiles code. The script ends with `done` and tells you to log in as `meshterm`. That user
has no password yet. Thus set one first:

```bash
# on the PicoCalc, as root
passwd meshterm
```

> **If no Wi-Fi dongle is attached, the next boot can take up to approximately eleven
> minutes.** The Wi-Fi kick tries for approximately six minutes before it stops. Then the
> time sync waits up to five more minutes for a network route. Nothing is broken. If the
> PicoCalc will stay offline, refer to [Day-to-day](#day-to-day) to switch both off.

### Step 10: Log in as meshterm

1. Log out of `root`.
2. Log in as `meshterm` with the password that you just set.
3. Try the app:

```bash
# on the PicoCalc, as meshterm
meshterm --mock
```

The full-screen interface of MeshTerm must start at **53 columns**, with a simulated radio
behind it. It must draw braille charts and node glyphs cleanly, with no empty boxes. Along
the bottom, you must see the **F-key lane**: five labelled chips, not the footer hint line
that a desktop terminal shows.

Look around in the app. **Esc** goes back from any screen. To leave the app, choose **Quit**
on the main menu, or hold **Esc** for three seconds from any screen. After one second, a box
shows a bar that fills. If you release **Esc** before the bar is full, you stay where you
were.

Then look at the whole visual language on one card:

```bash
# on the PicoCalc, as meshterm
meshterm specimen
```

Two console fonts are available: a 6×12 font (53 × 26) and a 6×8 font (53 × 40, which has
more rows but no Cyrillic glyphs). Try both from *Preferences → Display → Console font*.
When you select a font, MeshTerm shows it at once. The screen is painted again with the new
row count, so you can see the result before you keep the choice.

---

## Phase 3: Add the radio

You can do this phase later. The steps above already give you a working MeshTerm in `--mock`
mode. Come back to this phase when you have the soldering iron out.

### Step 11: Install PlatformIO

PlatformIO builds the firmware and loads it onto the XIAO. If you already have it (for
example, the PlatformIO extension of VS Code), go to Step 12.

On your PC, install PlatformIO with its
[official installer script](https://docs.platformio.org/en/latest/core/installation/methods/installer-script.html).
The script puts PlatformIO in a Python environment of its own, `~/.platformio/penv`, and
does not change your system Python:

```bash
# on the PC (Git Bash or WSL on Windows)
curl -fsSL -o get-platformio.py https://raw.githubusercontent.com/platformio/platformio-core-installer/master/get-platformio.py
python3 get-platformio.py          # on Windows: py get-platformio.py
```

The installer ends with the full path of `platformio`, and it tells you to add its directory
to your PATH. You do not have to. `build-firmware.sh` and `flash.py` look for PlatformIO in
`~/.platformio/penv` when it is not on the PATH.

Do not use `pip install platformio`. Many systems refuse a `pip install` into the system
Python (Debian, Ubuntu, and Homebrew mark it "externally managed"). Also, in a shell with an
active virtual environment, the package goes into that environment.

The first firmware build downloads the nRF52 toolchain, which is several hundred
megabytes. Do this where you have a good connection.

### Step 12: Build the firmware

```bash
# on the PC (Git Bash or WSL on Windows), from your MeshTerm checkout
cd scripts/picocalc-lyra/xiao-radio
sh build-firmware.sh
```

The script clones MeshCore, applies two patches, and builds. The first build takes several
minutes. The script ends with this output:

```
DONE.  Firmware: /path/to/scripts/picocalc-lyra/xiao-radio/meshcore-xiao-radio.uf2
Next: connect the USB-C port of the XIAO to this machine, and run:  python3 flash.py
      (on Windows: py flash.py)
```

These are the actions of the script. You can do them by hand if you must:

1. Clone [MeshCore](https://github.com/meshcore-dev/MeshCore) into
   `scripts/picocalc-lyra/xiao-radio/_meshcore-build`.
2. Check out the pinned commit `e9edfc8e` on the `dev` branch. This is the commit that the
   patches apply to and build against without a problem.
3. Apply `meshcore-uart1.patch`, then `meshcore-frame-timeout.patch`.
4. Build the `Xiao_nrf52_companion_radio_serial` PlatformIO environment:
   `pio run -e Xiao_nrf52_companion_radio_serial`.
5. Convert the resulting `.hex` file to a `.uf2` file with MeshCore's own converter.
   Use the UF2 family id of the nRF52840:
   `python bin/uf2conv/uf2conv.py firmware.hex -c -f 0xADA52840 -o meshcore-xiao-radio.uf2`.

#### What the patches change, and why they are still necessary

`meshcore-uart1.patch` changes two files. Both changes are necessary:

- **`examples/companion_radio/main.cpp`.** The companion-over-serial code of MeshCore
  declares `HardwareSerial companion_serial(1)`. This is a numbered constructor. On the
  Adafruit nRF52 core, `HardwareSerial` is an abstract base class and has no numbered
  constructor. Thus this line does not compile for the XIAO nRF52840. The patch adds an
  `#if defined(NRF52_PLATFORM)` branch. This branch binds `companion_serial` to `Serial1`
  instead. `Serial1` is the concrete UART object that the nRF52 core always provides.
  `Uart::setPins(rx, tx)` is called later in the same file. It takes the same arguments in
  both cases, thus nothing else needs to change.
- **`variants/xiao_nrf52/platformio.ini`.** This change adds a new
  `Xiao_nrf52_companion_radio_serial` build environment. The environment puts the companion
  serial link on pins **D6 (TX)** and **D7 (RX)**. It also **moves I²C off those same two
  pads**, to two internal pins, 16 and 17, that nothing in this build uses. This second
  change is the important one. The stock `Xiao_nrf52` environment maps I²C to D6/D7 by
  default. The `begin()` of the board and the sensor code both start the I²C bus on these
  pins automatically. Without the move, I²C takes the UART pins at boot. The wiring is
  correct, and the firmware builds and runs. But the serial link is completely silent,
  because another function already owns the pins.

We submitted both changes to MeshCore upstream. At the time of writing, they are **not
merged**, and there is no sign that this will happen soon. The upstream `dev` branch still
declares the bare `HardwareSerial(1)` with no nRF52 branch. The `platformio.ini` of the XIAO
still has no `_serial` companion environment. To build with the patch is the normal route
here, not a temporary measure. When the patch is merged, you can delete the `git apply`
step and the patch file.

`meshcore-frame-timeout.patch` changes the serial parser of the companion,
`src/helpers/ArduinoSerialInterface.cpp` and its header. Each protocol frame starts with `<`
and a length of two bytes, and the parser then reads that number of bytes. Without the
patch, the parser has no time limit. If one byte is lost or damaged on the wire, the parser
can read a wrong length of up to 65,535 bytes. Then it ignores each command until that many
bytes come in. MeshTerm sends one short request when it connects. Thus the radio does not
answer for many starts in sequence, and only a power cycle of the XIAO clears the fault.

With the patch, the parser drops a frame when its bytes stop for 200 ms, and the next
command starts a new frame. Bytes that wait in the receive buffer while the firmware is
busy still belong to the frame. Only silence on the wire resets the parser. All the serial
companions of MeshCore have this parser, thus the patch is also a candidate for upstream.
We have not submitted it yet.

#### Building by hand

If you want to follow a newer MeshCore and not the pinned commit, run:

```bash
# on the PC
git clone https://github.com/meshcore-dev/MeshCore.git
cd MeshCore
git checkout dev                            # or MESHCORE_COMMIT=dev sh build-firmware.sh
git apply /path/to/scripts/picocalc-lyra/xiao-radio/meshcore-uart1.patch
git apply /path/to/scripts/picocalc-lyra/xiao-radio/meshcore-frame-timeout.patch
pio run -e Xiao_nrf52_companion_radio_serial
python bin/uf2conv/uf2conv.py .pio/build/Xiao_nrf52_companion_radio_serial/firmware.hex \
    -c -f 0xADA52840 -o meshcore-xiao-radio.uf2
```

If `git apply` fails, upstream has changed and the context of the patch does not match. If
`meshcore-frame-timeout.patch` fails, open it and make its changes by hand in the two files
that it names. The two hunks of `meshcore-uart1.patch` are small, so you can also make them
by hand:

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

Only the pinned commit (`e9edfc8e`) is known to build without a problem with these patches. A
newer `dev` can need small changes.

### Step 13: Flash the XIAO

CAUTION: Make sure that the XIAO is the board that you flash and that it has the S140 v7
bootloader. A flash to the wrong board, or to the wrong SoftDevice, can leave a board that
does not start. `flash.py` checks both before it writes to a UF2 drive. Through serial DFU,
it accepts only the USB ids of the XIAO bootloader.

1. Connect the **USB-C port of the XIAO itself** to your PC. Do this before you wire the
   XIAO in Step 14. To flash a XIAO that is already wired, follow "Updating the radio
   firmware" in [Day-to-day](#day-to-day) instead. The USB power of a wired XIAO goes
   backward into the Lyra.
2. Run the flash tool, in the same directory as Step 12:

```bash
# on the PC (Git Bash or WSL on Windows)
python3 flash.py          # on Windows: py flash.py
```

The tool first tries a 1200-baud "touch" over USB serial. This resets the XIAO into its
bootloader. The touch targets only the USB vendor id of Seeed. Thus it does not disturb
another nRF52840 board on the same bench. If the touch does not work, **double-tap the reset
button of the XIAO** and run the tool again.

The bootloader then shows a UF2 drive, or only a serial port. When it shows a drive, the
tool checks two things before it writes anything:

- **The `Model` field of `INFO_UF2.TXT`** must say a XIAO. A drive letter is not an
  identity. Another board can have the letter that your XIAO just gave up.
- **`SoftDevice` must not be `6.1.1`.** This firmware links for SoftDevice S140 v7 (the app
  is at `0x27000`). An S140 6.1.1 bootloader wants the app at `0x26000`. The flash then
  succeeds, but the board never starts.

If you are certain, `--force` overrides both checks. A good flash ends with this output:

```
DONE. XIAO flashed and rebooting into the radio firmware.
```

**When the bootloader shows only a serial port**, the tool loads the firmware through serial
DFU itself. The XIAO that we tested has this type of bootloader. The tool runs the
PlatformIO upload in `_meshcore-build`, which sends the firmware that Step 12 built. This
takes approximately one minute. The USB ids of the port identify the XIAO bootloader. The
upload ends with these lines:

```
Device programmed.
...
DONE. XIAO flashed and rebooting into the radio firmware.
```

After the flash, the USB serial port of the XIAO is **silent** to companion frames. The
companion protocol is now on D6/D7, not on USB. This silence is correct. It does not mean
that the flash failed.

### Step 14: Wire the radio

You solder four wires. TX and RX are crossed:

| XIAO pad | → | Lyra header | Physical pin | Carries |
| --- | --- | --- | --- | --- |
| **D7** (Serial1 RX) | → | **GP4** | pin **6** | Lyra UART1 **TX** → XIAO RX |
| **D6** (Serial1 TX) | → | **GP5** | pin **7** | XIAO **TX** → Lyra UART1 RX |
| **GND** | → | **GND** | pin **8** | common ground |
| **3V3** | → | **3V3 OUT** | pin **36** | power, so that it runs without USB |

> **The Lyra does not use the GPIO numbering of the Raspberry Pi Pico.** The 40-pin header
> has the physical shape of the Pico header. But the function of each pad is the RK3506
> mapping of Luckfox. GP4 is `gpio0-0` and GP5 is `gpio0-1`. Do not use a Pico pinout for
> other pins on this header. Each signal here is 3.3 V, and this board has **no 5 V rail**.
> Power the XIAO from 3V3 only.

1. Turn the PicoCalc off and remove the back of the shell.
2. Remove the batteries.

WARNING: Put the soldering iron in its stand when you do not use it. The tip is hot enough
to burn your skin. Solder in a place that has good airflow.

CAUTION: Keep the iron on each pin only as long as necessary, and do not press down on the
board. Too much heat or force can damage the Lyra.

3. Solder the four wires between the pads of the XIAO and the header pins of the Lyra, as
   the table shows. The Lyra can stay in its socket. The tops of its header pins are
   accessible from inside the case, so solder to those.

CAUTION: Attach the LoRa antenna to the u.FL connector of the Wio-SX1262 before you turn
any power on. If you run the radio without an antenna, you can damage it.

4. Attach the LoRa antenna to the u.FL connector of the Wio-SX1262.
5. Wrap the XIAO + Wio-SX1262 stack in Kapton tape. Then no pad can touch the Lyra, the
   mainboard, or a battery.
6. Put the stack in the shell, in a place where it fits without strain on the wires.
7. Use a strip of foam tape to hold it against the inside of the case.

### Step 15: Set up the Lyra

1. Close the shell. Put the batteries back in.
2. If you did not do Step 8, copy `scripts/` to the Lyra now.
3. Run the setup script:

```bash
# on the PicoCalc, as root
sh scripts/picocalc-lyra/xiao-radio/lyra-setup.sh          # MT_USER=meshterm by default
```

You can run this script again without a problem. It does three things:

1. It installs `uart1-mux.py` as `/usr/local/bin/uart1-radio-mux.py`. A oneshot service,
   `uart1-radio-mux.service`, runs it at each boot. This part makes `/dev/ttyS1` reach the
   header pins. Calculinux enables the UART1 controller, but it never wires the controller
   to a physical pad. The flexible pin matrix of the RK3506 needs a direct register poke to
   route UART1 to GP4/GP5.
2. It adds `$MT_USER` to the `dialout` group, so that this user can open the serial port.
   **This change takes effect only after a new login.**
3. It writes a MeshTerm profile, *only if none exists yet*, at
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

If a `config.toml` already exists, the script does not change your file. It prints only the
`default_profile` line, the `[profiles.picocalc]` heading, and the `port`. Copy the whole
block above into your file yourself.

### Step 16: Run it

1. Log out and log in again as `meshterm`. Then the `dialout` group membership takes
   effect.
2. Start MeshTerm:

```bash
# on the PicoCalc, as meshterm
meshterm
```

A bare `meshterm` opens on the **Select a companion device** splash. The `picocalc` profile
is a row in the list. Select it. To skip the splash, run `meshterm -p picocalc`. Your node
must start, under the name that the firmware advertises. Both the device page and the
dashboard header show the name.

If you want to prove the wiring before you look for other causes, there is a deeper check.
Open `/dev/ttyS1` at 115200 as root. Send a raw `APP_START` frame (`3c 0e 00 01`, seven zero
bytes, then a name). A live radio answers with a framed `05`, `SELF_INFO`. The command byte
and the reply code are from the companion protocol. The `<` and `>` marks and the
little-endian length in the framing come from the firmware. Confirm them against the
firmware. This check is lower-level than a run of MeshTerm. It is mostly useful to find a
wiring problem.

---

## Day-to-day

**Charging.** Charge only through the USB-C port of the mainboard on the side of the case.
⚠ WARNING: Never charge through the USB-C of the Lyra at the back. Refer to
[Three ways to break it](#three-ways-to-break-it).

**Powering off.** Shut down the system. Do not pull the batteries:

```bash
# on the PicoCalc
sudo poweroff
```

**Updating MeshTerm.** The copy of `scripts/` that you made in Step 8 becomes old as soon as
the checkout changes. Thus run the scripts inside the checkout. You can pull and run the
setup script again from root. This reinstalls MeshTerm and builds the console font again:

```bash
# on the PicoCalc, as root
su - meshterm -c 'git -C ~/MeshTerm pull'
sh /home/meshterm/MeshTerm/scripts/picocalc-lyra/calculinux-setup.sh
```

Or, as the `meshterm` user, do only the update:

```bash
# on the PicoCalc, as meshterm
cd ~/MeshTerm && git pull && TMPDIR=$HOME/tmp .venv/bin/pip install -e .
```

If an update adds new glyphs to the console font, build the font again afterward:

```bash
# on the PicoCalc, as root
sh /home/meshterm/MeshTerm/scripts/picocalc-lyra/calculinux-console-font-6x12.sh
```

**Updating the radio firmware.** A new firmware for the XIAO goes in through its USB-C port,
as in Step 13. But the 3V3 wire of Step 14 connects the XIAO to the Lyra. When the XIAO gets
USB power, that wire powers the Lyra backward.

1. On the PC, build the firmware first ([Step 12](#step-12-build-the-firmware)). Then the
   cable stays connected only for the flash.
2. On the PicoCalc, run `sudo poweroff`. Then set the power switch to off.
3. Remove the back of the shell. Then remove the batteries.

CAUTION: Remove the batteries before you connect the USB-C port of the XIAO. Through the
3V3 wire, the USB power of the XIAO goes backward into the Lyra and the circuits next to
it.

4. Connect the USB-C port of the XIAO to your PC.
5. Run `flash.py` ([Step 13](#step-13-flash-the-xiao)).
6. When the tool shows `DONE`, disconnect the cable.
7. Put the batteries and the back of the shell in again. Then turn the PicoCalc on.

**Changing Wi-Fi.** Run `uwific` as root, as in [Step 7](#step-7-join-wi-fi). The kick at
boot time finds the new network by itself. It needs only that `iwd` knows the network.

**The slow boot without a dongle.** If the PicoCalc will always run offline, the Wi-Fi kick
and the time sync waste time at each boot. It is not enough to disable the kick, because the
time sync starts it again. Thus mask the kick. Then disable the time sync. Without this, the
time sync waits up to five minutes for a network:

```bash
# on the PicoCalc, as root
systemctl mask --now wifi-kick.service
systemctl disable --now time-sync.service
```

To undo this later, run `systemctl unmask wifi-kick.service` and
`systemctl enable time-sync.service`.

**The console font restores itself.** MeshTerm saves the font that the console used before
it starts. It puts that font back when it exits, after a normal quit and also after a crash.
You do not need to do anything to keep the shell's own font.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Nothing on the display of the PicoCalc | Is the SD card in the **Lyra's own slot**, not the front slot of the PicoCalc? Is a "Lyra B" (SPI NAND) erased? Is the board the **128 MB RAM, Pico-form-factor** variant? Other RAM sizes have pinouts that do not fit. |
| Wi-Fi never comes up | Is the dongle one of the tested chipsets (RTL8192CU / R8712U / RTL8188EU)? The USB port of the Lyra is **3.3 V**, thus a 5 V dongle does not work. On a cold boot, wait the full six minutes before you decide that it is stuck. |
| `pip install` fails with `ENOSPC` | `/tmp` is a small RAM disk. `calculinux-setup.sh` already sets `TMPDIR=$HOME/tmp` for its own install. If you run `pip` by hand, set it in the same way. |
| `meshterm: command not found` after setup | Log out and log in again. The PATH line is in `~/.profile`, which a new login shell reads. |
| Empty boxes instead of braille charts or node glyphs | The console font script did not run, or its result did not stay. Run `sh calculinux-console-font-6x12.sh` again. Check that `/etc/vconsole.conf` has a `FONT=` line. |
| `meshterm` cannot open `/dev/ttyS1` | Is the `meshterm` user in `dialout` (`groups`)? A group change takes effect only after a new login. |
| The port opens but the radio never answers | Flash again with the **`_serial`** environment, not `_ble` or `_usb`. Only `_serial` defines `SERIAL_RX`/`SERIAL_TX`. When the XIAO runs the radio firmware, its USB serial must be silent. This is expected and is not a fault. |
| Still silent, and the wiring is correct | Make sure that the firmware has the I²C remap (`PIN_WIRE_SCL=16`, `PIN_WIRE_SDA=17`). A build without the second hunk of the patch looks the same until you check this. |
| The flash was good, but the XIAO never comes back | The board or the SoftDevice is wrong. `INFO_UF2.TXT` must report a XIAO and **S140 v7**, not `6.1.1`. `flash.py` refuses to flash with either mismatch unless you use `--force`. |
| `flash.py` finds no UF2 drive | This is expected on a bootloader that has only a serial port. The tool then loads the firmware through serial DFU itself. If it says that there is no build, run `build-firmware.sh` first. |
| Nothing on `/dev/ttyS1` after a reboot | Run `systemctl status uart1-radio-mux`, then `journalctl -b -u uart1-radio-mux`. The pin-mux poke must run at each boot. If the oneshot fails, the log tells you why. |

---

## What has been tested

The maintainer tested this whole path, from a stock PicoCalc kit to a running radio, on one
bench. The bench had one Luckfox Lyra, one PicoCalc, and one XIAO + Wio-SX1262 pair. The path
works. A test in code checks one part: `tests/test_theme16.py` parses the font script and
compares it to the glyph inventory of MeshTerm. When the author wrote this guide, the author
checked these items by hand:

- the pinned MeshCore commit
- the need for the two hunks of the patch on `dev`
- the nRF52840 UF2 family id
- the XIAO bootloader ids, against the board files of Adafruit

On 2026-10-08, the author flashed the radio again, with the frame timeout, and checked these
items on the bench:

- the build and the flash with the commands of this guide, on a Windows PC that did not
  have the PlatformIO command-line tool
- that the bootloader of this XIAO shows only a serial port, and that `flash.py` then loads
  the firmware through serial DFU
- that the USB power of a wired XIAO goes backward into the Lyra
- that the radio keeps its identity through the flash
- that, after the flash, a frame that breaks off no longer blocks the link: the next
  `APP_START` after 0.5 s of silence gets its reply

The other facts are the findings of one bench. A second device can give other results:

- **The RK3506 register map** in `uart1-mux.py`. This covers the two register blocks, the
  UART1 signal ids, the matrix mode, and the fact that `gpio0-0`/`gpio0-1` are header GP4 and
  GP5. The author probed it live on one board. It is not from a reference manual.
- **That the Calculinux kernel permits those `/dev/mem` writes**, and that the mux oneshot
  really runs at each boot.
- **The header pin numbers** 6, 7, 8, and 36 for GP4, GP5, GND, and 3V3. The numbering on the
  Pico side is correct. The wiring of the carrier is an observation from the bench.
- **The pin order of the MX1.25 socket.** It comes from the wire colours of Luckfox's own cable.
  For this reason, [Step 3](#step-3-attach-the-wi-fi-dongle) tells you to check
  pin 1 with a meter.
- **The 5 V path through the USB-C of the Lyra.** The author read the mechanism from the
  mainboard schematic and from two forum measurements on the Pico. Nobody measured it with a
  Lyra. Use the warning as the safe assumption that it is.
- **The facts of Calculinux that the scripts assert.** These are the reduced Python and the
  package names that repair it, which overlays are writable, that `/tmp` is a small RAM
  tmpfs, the race of the dongle with `iwd` at cold boot, the occasional 404 of the opkg feed,
  that the stock Terminus console font is installed, and that `kbd` brings `setvtrgb`.
- **That this board has no RTC.** Only the script says this. The clock-sync phase follows
  from it.
- **The XIAO bootloader that showed no mass storage**, and the S140 v7 / `0x27000`
  requirement. Public references agree, but none is a primary source.
- **Whether the firmware patch still applies at the HEAD of MeshCore `dev`.** Only the pinned
  commit is known to build.
- **The framing of MeshCore's serial protocol** around the raw `APP_START` check in
  [Step 16](#step-16-run-it). The command byte and the reply code come from the protocol
  documentation. The framing bytes do not.
- **The 512-glyph limit of the framebuffer console.** It is well known. The author accepted
  it and did not measure it again.

If you build this machine and a result is different from this guide, please report it. A
second board is the only way to learn which parts are about the hardware in general and which
parts are about the one unit that the author used.
