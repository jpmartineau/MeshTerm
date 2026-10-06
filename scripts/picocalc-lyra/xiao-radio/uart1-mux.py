#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Route the RK3506 UART1 controller to the PicoCalc header pads GP4/GP5.

Calculinux enables the UART1 controller (so ``/dev/ttyS1`` exists) but never wires it to any
physical pad. The RK3506 "matrix IO" can carry UART1 on almost any pad; this poke selects it
on gpio0-0 (TX) and gpio0-1 (RX), which are the PicoCalc header holes GP4 and GP5 — where the
XIAO's D7 (RX) and D6 (TX) are soldered.

Two register groups, both write-masked (high 16 bits = write-enable mask):

  * RMIO signal select  @ 0xff910080 + pin*4  -> uart1-tx = 0x01, uart1-rx = 0x02
    (the signal id is the pinctrl "func" value minus 0x0f; verified against 8 live pins)
  * primary iomux       @ 0xff950000 + (pin//4)*4, 4-bit field (pin%4)*4 -> matrix mode = 7

Run as root, every boot (installed as the ``uart1-radio-mux`` systemd service by
``lyra-setup.sh``). Idempotent. After it runs, the XIAO radio is reachable at
``/dev/ttyS1`` @ 115200 8N1.
"""

import mmap
import os
import struct

RMIO = 0xFF910000  # matrix-IO signal-select block
PMU = 0xFF950000  # gpio0 primary iomux block (PMU IOC)

# gpio0-0 = RM_IO0 = PicoCalc GP4 (wired to XIAO D7 / UART RX)  -> carry UART1 TX
# gpio0-1 = RM_IO1 = PicoCalc GP5 (wired to XIAO D6 / UART TX)  -> carry UART1 RX
UART1_TX_SIG = 0x01
UART1_RX_SIG = 0x02


def poke(addr: int, value: int) -> None:
    """Write one 32-bit word to a physical address, through a one-page /dev/mem map.

    Needs root, and the address must be a register: this maps the page it falls in and
    writes straight to it.
    """
    page = addr & ~0xFFF
    off = addr - page
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        mm = mmap.mmap(fd, 0x1000, offset=page)
        try:
            mm[off : off + 4] = struct.pack("<I", value & 0xFFFFFFFF)
        finally:
            mm.close()
    finally:
        os.close(fd)


def main() -> None:
    """Point the two pads at UART1 and switch them to matrix mode.

    The four writes are the whole job, and re-running them changes nothing, so this is
    safe to run on every boot. See the module docstring for the register layout.
    """
    poke(RMIO + 0x80 + 0 * 4, (0x7F << 16) | UART1_TX_SIG)  # gpio0-0 -> UART1 TX
    poke(RMIO + 0x80 + 1 * 4, (0x7F << 16) | UART1_RX_SIG)  # gpio0-1 -> UART1 RX
    poke(PMU + 0x00, (0xFF << 16) | 0x77)  # iomux pins 0,1 -> matrix mode (7)
    print("UART1 routed to GP4/GP5 -> radio on /dev/ttyS1 @115200")


if __name__ == "__main__":
    main()
