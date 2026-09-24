# MeshTerm *v{version}*

**MeshTerm** for **MeshCore**.

## What it is

**MeshTerm** is a full-featured TUI **MeshCore** client for your terminal. Plug in a companion device over USB, pair one over Bluetooth, or reach one over TCP. Windows, macOS, and Linux. Free and open-source.

Nearly everything MeshTerm can do through its interactive Text-based User Interface *(TUI)*, it can also do from the Command-Line Interface *(CLI)*. The live pictures stay in the TUI, where they belong — the map, the dashboard, the live feed, the watchtower, the mesh walk — and everything else has a command. The TUI runs the same code as the CLI, so you get the best of both worlds: a great user experience, and the ability to script or schedule tasks! Every command prints for a person by default, and speaks JSON with `--json` when a program is reading!

While MeshTerm is running and connected to your radio, everything the radio overhears is recorded to a local database — adverts, telemetry, signal quality, positions, etc. Features like maps, dashboard, and route graphs read back from it, so the longer it runs, the more it knows about the mesh!

## Where it came from

The project had humble beginnings as a set of scripts in early 2026. As time passed, quality-of-life features were added, and at some point, the logical next step was to build a complete MeshCore client with all the features I felt were missing in other offerings.

I wanted something I could run from my computer. I wanted it to run on Windows, macOS, and Linux, and I wanted it to work with any MeshCore companion device, via Bluetooth, USB, or TCP. I wanted a mouseless keyboard-driven MeshCore experience. I wanted something retro-futuristic. I wanted to push the envelope of what can be done on a text terminal, while keeping system requirements low so it could run on an older computer or a lower-end System on a Chip *(SoC)*.

## Platform-specific developments

In addition to a generic terminal setup, I wanted it to run on two devices I had lying around in my nerdcave.

### ClockworkPi uConsole
MeshTerm is compatible with the **HackerGadgets AIO board (v1 or v2)**. The **AIO** board has just a raw LoRa radio, meaning it can't run MeshCore firmware. MeshTerm can run the mesh node in software itself, straight over the board's SPI bus, for as long as MeshTerm is running — no separate service needed. If you'd rather the node stayed on the mesh while MeshTerm is closed, an optional bridge service can run it all the time instead.

MeshTerm feels right at home on the **uConsole**. Running MeshTerm on it makes it feel like a piece of gear straight out of a William Gibson novel.

### ClockworkPi PicoCalc
Getting MeshTerm to run on the **PicoCalc** is a bit more involved, but totally worth it:
- Swap out the **Raspberry Pi Pico** for a **Luckfox Lyra** to run **Calculinux**.
- Add a compatible 3.3 V USB Wi-Fi dongle (e.g. **TP-Link TL-WN725N N150 Nano**) to the **Lyra**'s internal USB port.
- Add a LoRa device. Here are two options:
    - Connect a companion via USB-C;
    - add **Seeed Studio XIAO nRF52840 + Wio-SX1262** internally, soldered to GPIO connectors' legs.
- In order to use the XIAO nRF52840 via GPIO, a modified firmware is needed. A **PR** *https://github.com/meshcore-dev/MeshCore/pull/3191* has been submitted to MeshCore, but it might take a while for it to go through.

The UX is a bit different on the PicoCalc's 16-colour 53-column screen layout, but it's been tailored to make good use of the available space and the PicoCalc's F-keys. The result is, in my humble opinion, the best MeshCore experience you can get on a portable device. It runs on one 18650 battery, and a second one roughly doubles the runtime.

## What the future holds

MeshTerm will evolve over time. It is a work in progress.

I've built MeshTerm for myself — I find that's when I produce my best work — but I sincerely hope it will be appreciated by others.

If you'd like a specific feature, pitch your idea on the Discord!

## Licence

MeshTerm ships under the **Apache License, Version 2.0** — free to use,
change, and redistribute, commercially or otherwise, as long as the
LICENSE and NOTICE files travel with it. The full terms:
*https://www.apache.org/licenses/LICENSE-2.0*.

The licence does not cover the name: **MeshTerm and its logo are
trademarks the author keeps**, so a fork goes out under a name of its own.
The donate link in this app supports this project and its original
author — a fork that keeps soliciting through it, without pointing it at
itself, isn't affiliated with MeshTerm.

Built on **MeshCore** *https://meshcore.io/* — its firmware and companion
library — with Rich, prompt_toolkit, Typer, bleak, pyserial, pycryptodome,
markdown-it-py, tomli/tomli-w and Segno, each under its own terms.

Map data **© OpenStreetMap contributors**, served as vector tiles by
**OpenFreeMap** *https://openfreemap.org/* on the **© OpenMapTiles** schema
*https://openmaptiles.org/*, used under the **ODbL**
*https://www.openstreetmap.org/copyright*. That last address is where the map's
own corner credit points — nothing on a console is clickable, so the link is
spelled out here instead.

---

{copyright}
