#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Flash the built .uf2 onto the XIAO nRF52840 through its UF2 bootloader.

This script runs on your development machine. The USB-C port of the XIAO must be connected
to that machine. It works on Windows, Linux, and macOS.

The XIAO goes into bootloader mode in one of two ways. The first way is a physical
**double-tap of its reset button**. Then it mounts as the ``XIAO-SENSE`` drive. The second
way is for a XIAO that runs app firmware with a serial port. This script first tries an
automatic 1200-baud "touch" on that port.

Usage:
    python flash.py                       # flash ./meshcore-xiao-radio.uf2
    python flash.py path/to/firmware.uf2  # flash a specific file
"""

import glob
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def find_uf2_drive():
    """Return the mount path of a UF2 bootloader volume, or None."""
    candidates = []
    if os.name == "nt":
        import string

        candidates = [f"{d}:\\" for d in string.ascii_uppercase]
    else:
        for base in ("/media", "/run/media", "/mnt", "/Volumes"):
            candidates += glob.glob(os.path.join(base, "*"))
            candidates += glob.glob(os.path.join(base, "*", "*"))
    for c in candidates:
        try:
            if os.path.exists(os.path.join(c, "INFO_UF2.TXT")):
                return c
        except OSError:
            continue
    return None


def touch_1200():
    """Try to reset to the bootloader: open each Seeed XIAO app serial port at 1200 baud.

    The function matches only the USB vendor id of Seeed (2886). We did not add the Adafruit
    id 239A on purpose, although nRF52840 boards often use it. Other nRF52840 devices on the
    same bench (for example, a LilyGo T-Echo) use 239A. If the function puts the radio of
    another person into its bootloader, you find the wrong board in a bad way.
    """
    try:
        import serial
        import serial.tools.list_ports as lp
    except Exception:
        return
    for p in lp.comports():
        if "2886" in (p.hwid or ""):  # Seeed Studio USB VID
            try:
                serial.Serial(p.device, 1200).close()
                print(f"   sent 1200-baud bootloader touch to {p.device}")
                time.sleep(0.3)
            except Exception:
                pass


def bootloader_port():
    """Return the COM or tty device of a XIAO that is in its bootloader, or None."""
    try:
        import serial.tools.list_ports as lp
    except Exception:
        return None
    for p in lp.comports():
        hwid = (p.hwid or "").upper()
        # The Seeed VID and the bootloader PID: 0x0045 (Sense) or 0x0044 (plain nRF52840).
        # Both come from the board.h of the Adafruit UF2 bootloader for this chip family.
        if "2886" in hwid and ("0045" in hwid or "0044" in hwid):
            return p.device
    return None


def read_uf2_info(drive):
    """Parse INFO_UF2.TXT on ``drive`` into a dict of its ``Key: value`` lines."""
    info = {}
    try:
        with open(os.path.join(drive, "INFO_UF2.TXT")) as fh:
            for line in fh:
                if ":" in line:
                    k, _, v = line.partition(":")
                    info[k.strip()] = v.strip()
    except OSError:
        pass
    return info


def check_drive_is_ours(drive, force=False):
    """Refuse to write to a bootloader that is not the board that this firmware is built for.

    Two mistakes are easy to make and difficult to debug. The first is that a drive letter is
    not an identity. When one board leaves the bus, another board can get its letter. Thus the
    volume that you found can be different from the volume that you touched. The second is
    that this firmware links for SoftDevice S140 v7 (the app is at 0x27000). A board with
    S140 6.1.1 needs the app at 0x26000, and it does not boot with this firmware.
    """
    info = read_uf2_info(drive)
    model = info.get("Model", "?")
    softdev = info.get("SoftDevice", "?")
    print(f"   {drive} reports: {model} / {softdev}")

    problems = []
    if "xiao" not in model.lower():
        problems.append(f"this is a {model!r} bootloader, not a XIAO")
    if "6." in softdev:
        problems.append(f"{softdev} expects the app at 0x26000; this build is linked for S140 v7")
    if problems and not force:
        listed = "\n  - ".join(problems)
        sys.exit(
            f"ERROR: refusing to flash {drive} --\n  - {listed}\n"
            "Unplug the other board, or re-run with --force if you are sure."
        )
    return True


def main():
    """Put the XIAO in its bootloader and copy the firmware onto the drive that it shows.

    The function takes the .uf2 to flash. The default is the .uf2 that the build made next
    to this script. The function refuses to write to a drive that is a different board,
    unless the user gives ``--force``.
    """
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]
    uf2 = args[0] if args else os.path.join(HERE, "meshcore-xiao-radio.uf2")
    if not os.path.exists(uf2):
        sys.exit(f"ERROR: firmware not found: {uf2}\n(run build-firmware.sh first)")

    print(">> looking for XIAO bootloader drive ...")
    drive = find_uf2_drive()
    before = drive
    if not drive:
        touch_1200()
        for _ in range(30):
            drive = find_uf2_drive()
            if drive:
                break
            time.sleep(1)
    if drive and drive == before:
        # The letter was there before we touched anything, so it can belong to a different
        # board. Check what the drive reports. Do not trust the letter.
        print(f"   note: {drive} was already mounted before the touch")
    if not drive:
        port = bootloader_port()
        if port:
            sys.exit(
                f"ERROR: the XIAO is in its bootloader on {port} but exposes no UF2 drive.\n"
                "This bootloader presents a serial port only, so copy-to-drive can't work.\n"
                "Flash it over serial DFU instead, from your MeshCore checkout:\n"
                f"    pio run -e Xiao_nrf52_companion_radio_serial -t upload --upload-port {port}"
            )
        sys.exit(
            "ERROR: no UF2 bootloader drive and no XIAO bootloader serial port found.\n"
            "Double-tap the XIAO's reset button, then re-run."
        )

    check_drive_is_ours(drive, force=force)
    print(f">> flashing {os.path.basename(uf2)} -> {drive}")
    with open(uf2, "rb") as src:
        data = src.read()
    with open(os.path.join(drive, "firmware.uf2"), "wb") as dst:
        dst.write(data)
        dst.flush()
        os.fsync(dst.fileno())

    # After the write, the bootloader reboots into the app, and the drive goes away.
    for _ in range(10):
        if not os.path.exists(os.path.join(drive, "INFO_UF2.TXT")):
            print("DONE. XIAO flashed and rebooting into the radio firmware.")
            return
        time.sleep(1)
    print("DONE (copy complete). If it didn't reboot on its own, tap reset once.")


if __name__ == "__main__":
    main()
