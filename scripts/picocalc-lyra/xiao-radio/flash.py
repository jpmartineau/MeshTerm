#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Flash the built .uf2 onto the XIAO nRF52840 via its UF2 bootloader.

Runs on your DEV MACHINE with the XIAO's own USB-C plugged into it. Works on
Windows / Linux / macOS.

The XIAO enters bootloader mode either by a physical **double-tap of its reset button**
(it mounts as the ``XIAO-SENSE`` drive) or, if it is running app firmware that exposes a
serial port, by the automatic 1200-baud "touch" this script attempts first.

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
    """Best-effort: pulse any Seeed XIAO app serial port at 1200 baud to reset to bootloader.

    Matched on Seeed's USB vendor id (2886) *only*. Deliberately not widened to Adafruit's
    239A even though nRF52840 boards commonly use it: other nRF52840 gear on the same bench
    (a LilyGo T-Echo, say) answers to 239A, and knocking somebody else's radio into its
    bootloader is a rude way to find out you grabbed the wrong board.
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
    """Return the COM/tty device of a XIAO sitting in its bootloader, or None."""
    try:
        import serial.tools.list_ports as lp
    except Exception:
        return None
    for p in lp.comports():
        hwid = (p.hwid or "").upper()
        # Seeed VID + bootloader PID: 0x0045 (Sense) or 0x0044 (plain nRF52840), both from
        # Adafruit's UF2 bootloader board.h for this chip family.
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
    """Refuse to write to a bootloader that isn't the board this firmware was built for.

    Two different mistakes are cheap to make and expensive to debug. A drive letter is not an
    identity -- when one board leaves the bus another can inherit its letter, so the volume you
    found may not be the one you just touched. And this firmware links for SoftDevice S140 v7
    (app at 0x27000); a board carrying S140 6.1.1 wants it at 0x26000 and would simply not boot.
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
    """Put the XIAO in its bootloader and copy the firmware onto the drive it exposes.

    Takes the .uf2 to flash, defaulting to the one built beside this script, and
    refuses to write to a drive that turns out to be some other board unless
    ``--force`` says otherwise.
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
        # The letter was already there before we touched anything, so it may belong to some
        # other board entirely -- check what it says rather than trusting the letter.
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

    # the bootloader reboots into the app once written; the drive disappears
    for _ in range(10):
        if not os.path.exists(os.path.join(drive, "INFO_UF2.TXT")):
            print("DONE. XIAO flashed and rebooting into the radio firmware.")
            return
        time.sleep(1)
    print("DONE (copy complete). If it didn't reboot on its own, tap reset once.")


if __name__ == "__main__":
    main()
