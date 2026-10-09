# PicoCalc mesh radio: XIAO nRF52840 over UART

This is a Seeed XIAO nRF52840 with a Wio-SX1262. It is wired to UART1 of the Luckfox Lyra,
and it runs the MeshCore companion firmware on `Serial1`. Thus MeshTerm on the PicoCalc
opens `/dev/ttyS1` in the same way as for each other serial companion. This design does not
use BLE, does not use a USB host, and does not use the SD slot.

**The step-by-step guide is [`docs/devices/picocalc-lyra.md`, Phase 3](../../../docs/devices/picocalc-lyra.md#phase-3-add-the-radio).**
It has the parts, the firmware build and an explanation of the patch, the flashing, the
wiring, the setup of the Lyra, and what we proved on only one bench. The header of
`uart1-mux.py` explains what the register write does. This page is the bench card.

**Licensing.** The firmware is MeshCore, under the MIT License. The two patches
(`meshcore-uart1.patch` and `meshcore-frame-timeout.patch`) change the source of MeshCore, and `build-firmware.sh` makes a MeshCore binary. The licence
is [`LICENSE.MeshCore`](LICENSE.MeshCore) in this directory.

There are three steps on two machines:

```bash
# 1. dev machine: build (needs git + PlatformIO)
cd scripts/picocalc-lyra/xiao-radio && sh build-firmware.sh

# 2. dev machine: flash, with the XIAO's own USB-C plugged in. If the XIAO is already
#    wired, shut the PicoCalc down and remove its batteries first: the USB power of the
#    XIAO goes backward into the Lyra.
python3 flash.py           # on Windows: py flash.py

# 3. the Lyra, as root
sh scripts/picocalc-lyra/xiao-radio/lyra-setup.sh          # MT_USER=meshterm by default

# then, as that user
meshterm
```

There are four wires. Cross TX and RX:

| XIAO pad | → | Lyra header | Physical pin | Carries |
| --- | --- | --- | --- | --- |
| **D7** (Serial1 RX) | → | **GP4** | pin **6** | Lyra UART1 **TX** → XIAO RX |
| **D6** (Serial1 TX) | → | **GP5** | pin **7** | XIAO **TX** → Lyra UART1 RX |
| **GND** | → | **GND** | pin **8** | common ground |
| **3V3** | → | **3V3 OUT** | pin **36** | power, so it runs without USB |

> **The Lyra does not use the GPIO numbers of the Raspberry Pi Pico.** The header has the
> shape of a Pico header, but Luckfox made its own mapping. GP4 is `gpio0-0`, and GP5 is
> `gpio0-1`. All signals are 3.3 V, and there is **no 5 V rail**. Connect the power of the
> XIAO to 3V3 only.
