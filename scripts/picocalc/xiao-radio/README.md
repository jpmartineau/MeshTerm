# PicoCalc mesh radio — XIAO nRF52840 over UART

A Seeed XIAO nRF52840 + Wio-SX1262 wired to the Luckfox Lyra's UART1, running MeshCore
companion firmware on `Serial1`, so MeshTerm on the PicoCalc opens `/dev/ttyS1` like any
other serial companion. No BLE, no USB host, no SD-slot sacrifice.

**The step-by-step guide is [`docs/devices/picocalc-lyra.md`, Phase 3](../../../docs/devices/picocalc-lyra.md#phase-3--add-the-radio)** — parts,
the firmware build and the patch explained, flashing, wiring, the Lyra setup, and what has
only been proven on one bench. What the register poke does is in `uart1-mux.py`'s own
header. This page is the bench card.

**Licensing.** The firmware is MeshCore, under the MIT License. The `meshcore-uart1.patch`
modifies MeshCore source and `build-firmware.sh` produces a MeshCore binary; the licence is
[`LICENSE.MeshCore`](LICENSE.MeshCore) in this directory.

Three steps on two machines:

```bash
# 1. dev machine: build (needs git + PlatformIO)
cd scripts/picocalc/xiao-radio && sh build-firmware.sh

# 2. dev machine: flash, with the XIAO's own USB-C plugged in
python flash.py            # double-tap reset if the touch doesn't take

# 3. the Lyra, as root
sh scripts/picocalc/xiao-radio/lyra-setup.sh          # MT_USER=meshterm by default

# then, as that user
meshterm
```

Four wires, TX and RX crossed:

| XIAO pad | → | Lyra header | Physical pin | Carries |
| --- | --- | --- | --- | --- |
| **D7** (Serial1 RX) | → | **GP4** | pin **6** | Lyra UART1 **TX** → XIAO RX |
| **D6** (Serial1 TX) | → | **GP5** | pin **7** | XIAO **TX** → Lyra UART1 RX |
| **GND** | → | **GND** | pin **8** | common ground |
| **3V3** | → | **3V3 OUT** | pin **36** | power, so it runs without USB |

> **The Lyra does not use Raspberry Pi Pico GPIO numbering** — the header is Pico-shaped, the
> mapping is Luckfox's own. GP4 is `gpio0-0`, GP5 is `gpio0-1`. All signals are 3.3 V and
> there is **no 5 V rail**: power the XIAO from 3V3 only.
