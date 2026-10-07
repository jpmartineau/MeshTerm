#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Route the RK3506 UART1 controller to the PicoCalc header pads GP4/GP5.

Calculinux enables the UART1 controller (so ``/dev/ttyS1`` exists), but it does not connect
the controller to a physical pad. The "matrix IO" of the RK3506 can carry UART1 on almost
any pad. This poke selects UART1 on gpio0-0 (TX) and gpio0-1 (RX). These are the header
holes GP4 and GP5 of the PicoCalc, where D7 (RX) and D6 (TX) of the XIAO are soldered.

There are two register groups. Both are write-masked (the high 16 bits are the
write-enable mask):

  * RMIO signal select  @ 0xff910080 + pin*4  -> uart1-tx = 0x01, uart1-rx = 0x02
    (the signal id is the pinctrl "func" value minus 0x0f. We checked it on 8 live pins.)
  * primary iomux       @ 0xff950000 + (pin//4)*4, 4-bit field (pin%4)*4 -> matrix mode = 7

Run this script as root, at each boot. ``lyra-setup.sh`` installs it as the
``uart1-radio-mux`` systemd service. It is idempotent. After it runs, the XIAO radio is
available at ``/dev/ttyS1`` @ 115200 8N1.
"""

import mmap
import os
import struct

RMIO = 0xFF910000  # matrix-IO signal-select block
PMU = 0xFF950000  # gpio0 primary iomux block (PMU IOC)

# gpio0-0 = RM_IO0 = PicoCalc GP4 (wired to XIAO D7 / UART RX)  -> carries UART1 TX
# gpio0-1 = RM_IO1 = PicoCalc GP5 (wired to XIAO D6 / UART TX)  -> carries UART1 RX
UART1_TX_SIG = 0x01
UART1_RX_SIG = 0x02


def poke(addr: int, value: int) -> None:
    """Write one 32-bit word to a physical address, with a one-page /dev/mem map.

    This function must run as root, and the address must be a register. The function maps the
    page that has the address, and writes directly to it.
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

    The three writes are the whole job. A second run changes nothing, so it is safe to run
    this function at each boot. Refer to the module docstring for the register layout.
    """
    poke(RMIO + 0x80 + 0 * 4, (0x7F << 16) | UART1_TX_SIG)  # gpio0-0 -> UART1 TX
    poke(RMIO + 0x80 + 1 * 4, (0x7F << 16) | UART1_RX_SIG)  # gpio0-1 -> UART1 RX
    poke(PMU + 0x00, (0xFF << 16) | 0x77)  # iomux pins 0,1 -> matrix mode (7)
    print("UART1 routed to GP4/GP5 -> radio on /dev/ttyS1 @115200")


if __name__ == "__main__":
    main()
