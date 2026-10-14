# MeshTerm on the Cardputer Zero

M5Stack makes three Cardputers. This page is about the **Cardputer Zero**. It has a
Raspberry Pi Compute Module 0 inside, and it runs Linux. The original Cardputer and the
Cardputer-Adv are ESP32 microcontroller boards. They cannot run MeshTerm, and nothing on
this page applies to them.

MeshTerm is an app in M5Stack's app store for the Cardputer Zero. It starts from the
launcher of the handheld and draws on its display. It can drive M5Stack's Cap LoRa-1262
radio directly, or it can use a MeshCore companion. MeshTerm supports both models of the
handheld: the Cardputer Zero and the Cardputer Zero Lite.

- [The device](#the-device)
- [What works](#what-works)
- [Installing it on the device](#installing-it-on-the-device)
- [Trying it in the emulator](#trying-it-in-the-emulator)
- [Radios](#radios)

---

## The device

| | |
| --- | --- |
| Models | Cardputer Zero (C154) and Cardputer Zero Lite (C155). MeshTerm supports both |
| Computer | Raspberry Pi Compute Module 0: four 1 GHz cores, 512 MB of memory |
| Screen | 1.9 inches, 320×170 pixels |
| Keyboard | 46 keys, with Shift, Fn and Sym layers |
| System | Raspberry Pi OS "Trixie", with M5Stack's own app launcher |

The two models have the same computer, display, keyboard, battery, and expansion header.
The Cardputer Zero also has a camera, motion sensors, and a 32 GB microSD card. MeshTerm
uses none of these three parts, thus it works in the same way on both models. The Lite
comes without a microSD card. It needs a card with M5Stack's system on it before it can
start (refer to [M5Stack's documentation](https://docs.m5stack.com/en/CardputerZero)).

MeshTerm draws the display in 53 columns by 14 rows. The width is the same as on the
PicoCalc, but the height is approximately half. Thus many screens have a more compact
layout here.

The launcher gives an app the whole display and the keyboard, but it gives no text console
to run in. Thus MeshTerm has its own console. This is a small program inside MeshTerm. It
draws each character on the display itself and reads the keys directly.

## What works

All of these were tried on a real Cardputer Zero:

- MeshTerm starts from M5Stack's app launcher and draws on the display itself.
- MeshTerm has a layout for the display of 14 rows. The title bar also shows the unread
  count and the battery. The tab strips have one row. The dialogs fit the short display.
- The battery in the title bar is the Cardputer Zero's own battery.
- The row of function-key labels is along the bottom of the display. **Fn+4** to **Fn+8**
  drive it. These keys are directly under the display. **Shift** with them gives a second
  set.
- M5Stack's Cap LoRa-1262 radio add-on (refer to [Radios](#radios)).
- Hold **Esc** to quit. This is how the launcher closes each app. After one second,
  MeshTerm shows a box that says so. The box has a bar that fills over the next two
  seconds. While the box is visible, MeshTerm sends nothing new over the radio. If you
  release **Esc** before the bar is full, you return to the screen that you were on. If you
  keep holding it, MeshTerm saves everything and closes in good time, before the launcher
  forces it. A short press of **Esc** still goes back one screen, as on all other
  handhelds.

## Installing it on the device

### From the app store

1. On the Cardputer Zero, open M5Stack's app store.
2. Find **MeshTerm**. It is in the **Radio & Comms** category. Its share code is `MTRM`.
3. Install it.

MeshTerm now has an icon in the launcher. The package has all that MeshTerm needs: its
Python libraries, the radio library for the Cap, and the font of the display. Each new
version of MeshTerm also goes to the store.

The store gets a new version a short time after its release, when the check of the store
passes. If you want a new version before the store has it, install the same package from
GitHub.

### From GitHub

Each [release of MeshTerm](https://github.com/jpmartineau/MeshTerm/releases/latest) has the
package that the store has, `meshterm_<version>-1_arm64.deb`. Copy it to the Cardputer
Zero, for example with `scp`. Then install it from a command line on the handheld:

```bash
sudo apt install ./meshterm_*_arm64.deb
```

To remove it, run `sudo apt remove meshterm`.

### From the source code

This way is for developers who know a Linux command line. The Cardputer Zero must be on
Wi-Fi. You also need a way to type commands on it. SSH from another computer is the
easiest way.

1. Get MeshTerm and the radio library, and download the font that the display uses:

```bash
git clone https://github.com/jpmartineau/MeshTerm.git
cd MeshTerm
python3 -m venv .venv
.venv/bin/pip install -e '.[spi]'
.venv/bin/meshterm emulate --fetch-fonts
```

2. Put MeshTerm in the launcher. The command asks for your password one time, because the
   list of apps of the launcher belongs to the system:

```bash
scripts/cardputer-zero/launcher-entry.sh
```

MeshTerm now has an icon in the launcher. The icon is a negative of the MeshTerm logo. Thus
it is different from the icon of MeshTerm from the app store, and you can have both. To
remove the icon, run `scripts/cardputer-zero/launcher-entry.sh remove`.

### Typing long text from your computer

A long password or key is hard to type on a keyboard this small. Use another computer that
can reach the Cardputer over SSH. The tool `scripts/cardputer-zero/type-text.py` types the
text for you, into the app that has the display:

```bash
python scripts/cardputer-zero/type-text.py --host pi@cardputer.local
```

1. On the Cardputer, open the field that you want to fill.
2. Run the command above on your computer.
3. Paste the text when the tool asks for it, and press Enter. The tool does not show your
   typing. The text goes to the Cardputer over SSH, thus it does not show on either
   display.
4. Do not touch the Cardputer's keyboard until the tool says that it is done.

## Trying it in the emulator

The emulator opens a window that shows the display of the Cardputer Zero. The window
scales the display up so that you can read it. If you run it with a simulated radio, you
can try the layout without the handheld and without a radio.

The emulator is not a strict emulator. It does not copy the computer of the Cardputer
Zero. MeshTerm runs on your own computer, at the speed of your computer. The emulator
copies the constraints of the display and the keyboard: the size of the display in pixels,
the font, the colours, and the keys that drive the labels along the bottom. This shows what
fits on the display and what does not. That is the purpose of the emulator.

The emulator needs MeshTerm installed with pip or pipx (refer to the
[README](../../README.md)). The one-file downloads do not have the part of Python that
opens windows.

1. Download the font that the display uses. Do this one time:

```bash
meshterm emulate --fetch-fonts
```

2. Start the emulator:

```bash
meshterm emulate cardputer-zero --mock
```

`--mock` is the simulated radio. Before you start, set `MESHTERM_HOME` to an empty folder.
Then the made-up nodes of the simulation stay out of your real history. `--scale 4` makes
the window bigger.

In the window, the **F4** to **F8** keys of your keyboard replace **Fn+4** to **Fn+8**.
Hold **Shift** to show the second set. You can also click the keys. Click the keys that are
drawn under the display, or the coloured keys along its bottom row. Hold **Shift** while
you click for the second set. **Ctrl+Shift+S** saves a screenshot at the real size of the
display. When you close the window, MeshTerm quits. It also quits when you hold **Esc** for
three seconds, as it does on the handheld.

## Radios

The Cardputer Zero has no LoRa radio built in. You can add one in three ways:

| Radio | Status |
| --- | --- |
| **Cap LoRa-1262** add-on board from M5Stack | Works, with no setup. MeshTerm runs the mesh node itself while it is open, as it does on a uConsole. |
| A MeshCore companion on USB | Expected to work as on any Linux computer. |
| A MeshCore companion over Bluetooth | Expected to work as on any Linux computer. |

### The Cap LoRa-1262

**CAUTION:** Turn off the Cardputer Zero before you install or remove a Cap, also the Cap
LoRa-1262. M5Stack does not support hot swapping of a Cap on the Cardputer Zero. In
M5Stack's words, "hot-swapping may cause the device to malfunction" (refer to
[M5Stack's documentation](https://docs.m5stack.com/en/CardputerZero)).

**CAUTION:** Install the antenna on the Cap before you turn on the Cardputer Zero. M5Stack
says that power without the antenna can permanently damage the radio.

1. Turn off the Cardputer Zero.
2. Install the antenna on the Cap.
3. Push the Cap onto the expansion header at the back.
4. Turn on the Cardputer Zero, and start MeshTerm.

The device screen lists the Cap as **Cap LoRa-1262**. `meshterm --spi` also reaches it from the
command line.

The Cap has no MeshCore firmware of its own. It is only a radio chip. Thus, while MeshTerm
is open, MeshTerm is the node, as it is on the AIO board of a uConsole. MeshTerm needs the
radio library. The package from the store has it. If you install MeshTerm with pip,
install the library next to it:

```bash
pip install 'mesh-term[spi] @ git+https://github.com/jpmartineau/MeshTerm'
```

When MeshTerm connects, it does three things that the Cardputer Zero needs before the
radio answers:

- It switches on the power to the expansion header. The power is off until a program asks
  for it.
- It makes sure that two pins of the header connect to the radio and not to the USB port
  that shares them.
- It switches on the antenna path of the Cap.

When MeshTerm quits, it switches the power of the header off. Thus the Cap does not drain
the battery while nothing uses it. But this does not make it safe to remove the Cap. Turn
off the Cardputer Zero first.

A new node starts with the US/Canada settings of MeshCore (910.525 MHz). In other regions,
change the settings on **Device config** before you send anything. MeshTerm keeps the key,
the contacts, and the channels of the node in `~/.meshterm/radio/spidev0.1/`.

#### The Cap's GPS

The Cap also has a GPS. MeshTerm can use it to keep the position of your node current. The
GPS is off until you switch it on, as on MeshCore firmware. To switch it on:

1. Open **Device config**.
2. Under **Custom variables**, set **GPS** to on.
3. Apply.

When the GPS has a fix, the fix becomes the position of your node. In the open air, the
first fix usually takes approximately one minute. Indoors, the fix may never come.

Two things do not change:

- **You decide if your adverts include your position.** This is the **Share location**
  setting on Device config. If it is off, the GPS still moves your node on MeshTerm's own map,
  but nobody else sees where you are.
- **A hand move of your node on the map does not stay while the GPS is on.** The next fix
  puts the node back where the GPS says you are. Switch the GPS off first.

**GPS interval** sets how often the position changes, in seconds. The default is 0. This
means each fix, approximately one each second. MeshTerm remembers your last fix when it
quits. Thus the next start begins where you were.

The GPS talks to the Cardputer over a serial port, as a companion on a USB cable does. But
it is not a companion, thus the device screen does not list it.
