# MeshTerm on the Cardputer Zero

> [!CAUTION]
> **The Cardputer Zero is not supported yet.**
>
> MeshTerm now runs on a real Cardputer Zero, with M5Stack's Cap LoRa-1262 as its
> radio. But it runs only from a copy of the source code that you set up yourself. There is
> no package in M5Stack's app store. Also, nobody has typed on the keyboard. Its keys
> are checked only when you send key events to it from another computer.
>
> Do not buy a Cardputer Zero to run MeshTerm on it yet. This page will tell you when that
> changes.

M5Stack makes three Cardputers. This page is about the **Cardputer Zero**. It has a
Raspberry Pi Compute Module 0 inside, and it runs Linux. The original Cardputer and the
Cardputer-Adv are ESP32 microcontroller boards. They cannot run MeshTerm, and nothing on
this page applies to them.

- [The device](#the-device)
- [Where the work stands](#where-the-work-stands)
- [Installing it on the device](#installing-it-on-the-device)
- [Trying it in the emulator](#trying-it-in-the-emulator)
- [Radios](#radios)

---

## The device

| | |
| --- | --- |
| Computer | Raspberry Pi Compute Module 0: four 1 GHz cores, 512 MB of memory |
| Screen | 1.9 inches, 320×170 pixels |
| Keyboard | 46 keys, with Shift, Fn and Sym layers |
| System | Raspberry Pi OS "Trixie", with M5Stack's own app launcher |

MeshTerm draws the display in 53 columns by 14 rows. The width is the same as on the
PicoCalc, but the height is approximately half. Thus many screens have a more compact
layout here.

The launcher gives an app the whole display and the keyboard, but it gives no text console
to run in. Thus MeshTerm has its own console. This is a small program inside MeshTerm. It
draws each character on the display itself and reads the keys directly.

## Where the work stands

**Done, and tried on a real Cardputer Zero:**

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

**Not done:**

- A package for M5Stack's app store. Until there is one, you install MeshTerm
  yourself (refer to the next section).
- Typing on the real keyboard. Key events for the arrows, Enter, Esc, Fn+4 to Fn+8, and
  Shift were sent to the keyboard from another computer. They worked. The symbols on the
  Sym layer agree with M5Stack's own keymap on the handheld. Nobody has typed a
  message on it yet.

## Installing it on the device

This section is for users who know a Linux command line. The Cardputer Zero must be on
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

MeshTerm now has an icon in the launcher. To remove the icon, run
`scripts/cardputer-zero/launcher-entry.sh remove`.

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

1. Push the Cap onto the expansion header at the back.
2. Start MeshTerm.

The device screen lists the Cap as **Cap LoRa-1262**. `meshterm --spi` also reaches it from the
command line.

The Cap has no MeshCore firmware of its own. It is only a radio chip. Thus, while MeshTerm
is open, MeshTerm is the node, as it is on the AIO board of a uConsole. MeshTerm needs the
radio library, installed next to it:

```bash
pip install 'mesh-term[spi]'
```

When MeshTerm connects, it does three things that the Cardputer Zero needs before the
radio answers:

- It switches on the power to the expansion header. The power is off until a program asks
  for it.
- It makes sure that two pins of the header connect to the radio and not to the USB port
  that shares them.
- It switches on the antenna path of the Cap.

When MeshTerm quits, it switches the power of the header off. Thus the Cap does not drain
the battery while nothing uses it.

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
