# MeshTerm on the Cardputer Zero

> [!CAUTION]
> **The Cardputer Zero is not supported yet.**
>
> MeshTerm has never run on a real Cardputer Zero. Everything on this page was built from
> M5Stack's published source code and documentation, and tested only in a simulator on a
> desktop computer. There is no package to install on the device, the screen and keyboard
> code has never touched the hardware, and the LoRa add-on board is not wired up at all.
>
> Don't buy a Cardputer Zero to run MeshTerm on it. This page will say so when that
> changes.

M5Stack makes three Cardputers. This page is about the **Cardputer Zero**: the one with a
Raspberry Pi Compute Module 0 inside, running Linux. The original Cardputer and the
Cardputer-Adv are ESP32 microcontroller boards; they can't run MeshTerm, and nothing here
applies to them.

- [The device](#the-device)
- [Where the work stands](#where-the-work-stands)
- [Trying it in the simulator](#trying-it-in-the-simulator)
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

**Done, and tested only in the simulator:**

- MeshTerm's layout for the 14-row screen: a title bar that also carries the unread count
  and the battery, one-row tab strips, and dialogs sized for the short screen.
- The row of function-key labels along the bottom of the screen, driven by **Fn+4** to
  **Fn+8**, the keys that sit right under the display. **Shift** with them is a second set.
- The program that draws MeshTerm on the screen, and a desktop simulator built on it.

**Not done:**

- Running on the real device at all.
- A package for M5Stack's app store, so the launcher can start MeshTerm.
- The LoRa radio add-on (see [Radios](#radios)).
- Checking the keyboard on the device. Its layout was read from M5Stack's driver code,
  and two printings of the keyboard exist.

## Trying it in the simulator

The simulator opens a window showing the Cardputer Zero's screen, drawn pixel for pixel
the way the device would draw it, scaled up so it's readable. It runs MeshTerm with a
simulated radio, so you can try the layout without the device or a radio.

You need a copy of MeshTerm's source code (see
[CONTRIBUTING.md](../../CONTRIBUTING.md) for setting one up). Then, once, fetch the font
the screen is drawn in:

```bash
python scripts/cardputer-zero/fetch-terminus.py
```

and start the simulator:

```bash
python -m meshterm.host --mock
```

Point `MESHTERM_HOME` at an empty folder first, so the simulated radio's made-up nodes
stay out of your real history.

In the window, your keyboard's **F4** to **F8** stand in for **Fn+4** to **Fn+8**, and
holding **Shift** shows the second set. **Ctrl+Shift+S** saves a screenshot at the screen's
real size. Closing the window quits MeshTerm.

## Radios

The Cardputer Zero has no LoRa radio built in. Once MeshTerm runs on it, there will be
three ways to add one:

| Radio | Status |
| --- | --- |
| A MeshCore companion on USB | Expected to work as on any Linux computer. |
| A MeshCore companion over Bluetooth | Expected to work as on any Linux computer. |
| M5Stack's **Cap LoRa-1262** add-on board | Not wired up. Its pins on the Cardputer Zero are known, but MeshTerm still needs two setup steps it can't do yet: switching on the board's antenna chip, and freeing two of the expansion header's pins from the USB port that shares them. |
