# MeshTerm on the Cardputer Zero

> [!CAUTION]
> **The Cardputer Zero is not supported yet.**
>
> MeshTerm now runs on a real Cardputer Zero, with M5Stack's Cap LoRa-1262 as its radio,
> but only from a copy of the source code you set up yourself. There is no package in
> M5Stack's app store, and the keyboard has only been checked by sending key events to it
> from another computer, not by someone typing on it.
>
> Don't buy a Cardputer Zero to run MeshTerm on it yet. This page will say so when that
> changes.

M5Stack makes three Cardputers. This page is about the **Cardputer Zero**: the one with a
Raspberry Pi Compute Module 0 inside, running Linux. The original Cardputer and the
Cardputer-Adv are ESP32 microcontroller boards; they can't run MeshTerm, and nothing here
applies to them.

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

MeshTerm draws the screen in 53 columns by 14 rows: the same width as on the PicoCalc,
but only about half the height, so many screens have a more compact layout here.

The launcher gives an app the whole screen and the keyboard, but no text console to run
in. So MeshTerm brings its own: a small program inside MeshTerm that draws every
character on the screen itself and reads the keys directly.

## Where the work stands

**Done, and tried on a real Cardputer Zero:**

- MeshTerm starts from M5Stack's app launcher and draws on the screen itself.
- MeshTerm's layout for the 14-row screen: a title bar that also carries the unread count
  and the battery, one-row tab strips, and dialogs sized for the short screen.
- The battery in the title bar is the Cardputer Zero's own.
- The row of function-key labels along the bottom of the screen, driven by **Fn+4** to
  **Fn+8**, the keys that sit right under the display. **Shift** with them is a second set.
- M5Stack's Cap LoRa-1262 radio add-on (see [Radios](#radios)).
- Holding **Esc** for three seconds, which is how the launcher closes any app, closes
  MeshTerm properly.

**Not done:**

- A package for M5Stack's app store. Until there is one, you install MeshTerm yourself
  (see below).
- Typing on the real keyboard. The arrows, Enter, Esc, Fn+4 to Fn+8, and Shift have been
  checked by sending their key events to the keyboard from another computer, and the
  symbols on the Sym layer match M5Stack's own keymap on the device. Nobody has typed a
  message on it yet.

## Installing it on the device

This is for people comfortable with a Linux command line. You need the Cardputer Zero on
Wi-Fi, and a way to type commands on it: SSH from another computer is easiest.

Get MeshTerm and the radio library, and fetch the font the screen is drawn in:

```bash
git clone https://github.com/jpmartineau/MeshTerm.git
cd MeshTerm
python3 -m venv .venv
.venv/bin/pip install -e '.[spi]'
.venv/bin/meshterm emulate --fetch-fonts
```

Then put MeshTerm in the launcher. This asks for your password once, because the launcher's
list of apps belongs to the system:

```bash
scripts/cardputer-zero/launcher-entry.sh
```

MeshTerm now has an icon in the launcher. `scripts/cardputer-zero/launcher-entry.sh remove`
takes it out again.

### Typing long text from your computer

A long password or key is hard work on a keyboard this small. From another computer that
can reach the Cardputer over SSH, `scripts/cardputer-zero/type-text.py` types it for you,
into whatever app has the screen:

```bash
python scripts/cardputer-zero/type-text.py --host pi@cardputer.local
```

Paste the text when it asks, and press Enter. Your typing stays hidden, and the text goes
to the Cardputer over SSH, so it never shows on either screen. Open the field you want it
in on the Cardputer first, and leave its keyboard alone until the tool says it's done.

## Trying it in the emulator

The emulator opens a window showing the Cardputer Zero's screen, scaled up so it's
readable. Run it with a simulated radio, and you can try the layout without the device or
a radio.

It isn't a strict emulator. It doesn't pretend to be the Cardputer Zero's computer:
MeshTerm runs on your own computer, at your computer's speed. What it copies is the
screen and the keyboard — the screen's size in pixels, the font, the colours, and the keys
that drive the labels along the bottom. That's enough to show what fits on the screen and
what doesn't, which is what it's for.

It needs MeshTerm installed with pip or pipx (see the [README](../../README.md)). The
one-file downloads leave out the part of Python that opens windows.

Once, fetch the font the screen is drawn in:

```bash
meshterm emulate --fetch-fonts
```

Then start the emulator:

```bash
meshterm emulate cardputer-zero --mock
```

`--mock` is the simulated radio. Point `MESHTERM_HOME` at an empty folder first, so its
made-up nodes stay out of your real history. `--scale 4` makes the window bigger.

In the window, your keyboard's **F4** to **F8** stand in for **Fn+4** to **Fn+8**, and
holding **Shift** shows the second set. You can click the keys instead, either the ones
drawn under the screen or the coloured keys along its bottom row; hold **Shift** while you
click for the second set. **Ctrl+Shift+S** saves a screenshot at the screen's
real size. Closing the window quits MeshTerm.

## Radios

The Cardputer Zero has no LoRa radio built in. There are three ways to add one:

| Radio | Status |
| --- | --- |
| M5Stack's **Cap LoRa-1262** add-on board | Works, with no setup. MeshTerm runs the mesh node itself while it's open, the same way it does on a uConsole. |
| A MeshCore companion on USB | Expected to work as on any Linux computer. |
| A MeshCore companion over Bluetooth | Expected to work as on any Linux computer. |

### The Cap LoRa-1262

Push the Cap onto the expansion header at the back, then start MeshTerm. The device screen
lists it as **Cap LoRa-1262**, and `meshterm --spi` reaches it from the command line.

The Cap has no MeshCore firmware of its own: it's just a radio chip. So while MeshTerm is
open, MeshTerm *is* the node, the way it is on a uConsole's AIO board. It needs the radio
library installed beside MeshTerm:

```bash
pip install 'mesh-term[spi]'
```

When MeshTerm connects, it does three things the Cardputer Zero needs before the radio
answers. It switches on the power to the expansion header, which is off until something
asks for it. It makes sure two of the header's pins are connected to the radio and not to
the USB port that shares them. And it switches on the Cap's antenna path. When MeshTerm
quits, it switches the header's power back off, so the Cap doesn't drain the battery
while nothing is using it.

A new node starts on MeshCore's US/Canada settings (910.525 MHz). Anywhere else, change
them on **Device config** before you send anything. The node's key, contacts, and channels
are kept in `~/.meshterm/radio/spidev0.1/`.

#### The Cap's GPS

The Cap also carries a GPS, and MeshTerm can keep your node's position up to date from it.
It's off until you switch it on, the same as on MeshCore firmware. To switch it on:

1. Open **Device config**.
2. Under **Custom variables**, set **GPS** to on.
3. Apply.

Once the GPS has a fix, it becomes your node's position. Out in the open, the first fix
usually takes about a minute. Indoors, it may never come.

Two things don't change:

- **Whether your adverts include your position is still up to you.** That's the **Share
  location** setting on Device config. With it off, the GPS still moves your node on
  MeshTerm's own map, but nobody else sees where you are.
- **Moving your node on the map by hand doesn't stick while the GPS is on.** The next fix
  puts it back where the GPS says you are. Switch the GPS off first.

**GPS interval** sets how often the position is updated, in seconds. It's 0 to start with,
which means every fix, about once a second. MeshTerm remembers your last fix when it quits,
so the next start begins where you were.

The GPS talks to the Cardputer over a serial port, the way a companion on a USB cable
does. It isn't a companion, though, so the device screen doesn't list it.
