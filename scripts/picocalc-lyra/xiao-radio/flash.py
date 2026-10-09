#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Flash the built .uf2 onto the XIAO nRF52840 through its UF2 bootloader.

This script runs on your development machine. The USB-C port of the XIAO must be connected
to that machine. It works on Windows, Linux, and macOS.

The XIAO goes into bootloader mode in one of two ways. The first way is a physical
**double-tap of its reset button**. The second way is for a XIAO that runs app firmware
with a serial port. This script first tries an automatic 1200-baud "touch" on that port.

A XIAO bootloader shows one of two things. Some bootloaders mount a UF2 drive
(``XIAO-SENSE``), and the script copies the .uf2 file to it. Other bootloaders show only a
serial port. Then the script loads the firmware through serial DFU itself: it runs the
PlatformIO upload in the MeshCore checkout that ``build-firmware.sh`` built.

The script needs ``pyserial`` for the touch and for the search of the serial port. When the
Python that runs the script does not have it, the script runs itself again with the Python
of PlatformIO, which always has it.

Usage:
    python3 flash.py                       # flash ./meshcore-xiao-radio.uf2
    python3 flash.py path/to/firmware.uf2  # flash a specific file
"""

import glob
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

#: The PlatformIO environment that ``build-firmware.sh`` builds.
ENV = "Xiao_nrf52_companion_radio_serial"

#: The seconds that the script waits for a UF2 drive after the bootloader port shows. A
#: bootloader with a drive mounts it within this time. A bootloader without one never does.
DRIVE_GRACE_S = 5


def platformio_penv():
    """Return the directory of the PlatformIO environment, or None.

    The official installer and the PlatformIO extension of VS Code both put it in
    ``~/.platformio/penv``. The programs are in ``Scripts`` on Windows and in ``bin`` on
    other systems.
    """
    base = os.path.join(os.path.expanduser("~"), ".platformio", "penv")
    for sub in ("Scripts", "bin"):
        path = os.path.join(base, sub)
        if os.path.isdir(path):
            return path
    return None


def find_pio():
    """Return the path of the PlatformIO CLI: on the PATH first, else in its environment."""
    found = shutil.which("pio") or shutil.which("platformio")
    if found:
        return found
    penv = platformio_penv()
    if penv:
        for name in ("pio.exe", "pio"):
            path = os.path.join(penv, name)
            if os.path.isfile(path):
                return path
    return None


def ensure_pyserial():
    """Make sure that ``pyserial`` imports, or run this script again with a Python that has it.

    Without ``pyserial``, the touch and the search of the bootloader port cannot run. In the
    past, both stopped without a message, and the script then asked for a double-tap that
    could not help.
    """
    try:
        import serial  # noqa: F401 - only a check that the module exists

        return
    except ImportError:
        pass
    penv = platformio_penv()
    if penv:
        for name in ("python.exe", "python"):
            python = os.path.join(penv, name)
            same = os.path.abspath(python) == os.path.abspath(sys.executable)
            if os.path.isfile(python) and not same:
                print(f"   (no pyserial here, so the script runs again with {python})", flush=True)
                sys.exit(subprocess.call([python, os.path.abspath(__file__)] + sys.argv[1:]))
    sys.exit(
        "ERROR: flash.py needs pyserial. Install PlatformIO (Step 11), or run: pip install pyserial"
    )


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


def flash_serial_dfu(port, uf2_given=False):
    """Load the firmware through the serial DFU of a bootloader that shows no drive.

    The PlatformIO upload makes the DFU package from the build and sends it. Thus it loads
    the firmware that ``build-firmware.sh`` built in the MeshCore checkout, and not a .uf2
    file. The checkout is ``MESHCORE_DIR``, or else ``_meshcore-build`` next to this script,
    as in ``build-firmware.sh``.
    """
    checkout = os.environ.get("MESHCORE_DIR") or os.path.join(HERE, "_meshcore-build")
    command = ["run", "-e", ENV, "-t", "upload", "--upload-port", port]
    pio = find_pio()
    print(f">> the bootloader on {port} has no UF2 drive: loading through serial DFU")
    if uf2_given:
        print("   note: serial DFU loads the build in the MeshCore checkout, not the file you gave")
    if not pio or not os.path.isdir(os.path.join(checkout, ".pio", "build", ENV)):
        sys.exit(
            f"ERROR: no {'PlatformIO' if not pio else 'build in ' + checkout} for serial DFU.\n"
            "Run build-firmware.sh first. To load by hand, run in the MeshCore checkout:\n"
            f"    pio {' '.join(command)}"
        )
    sys.stdout.flush()  # keep this script's lines before the lines of the upload
    status = subprocess.call([pio] + command, cwd=checkout)
    if status != 0:
        sys.exit(f"ERROR: the serial DFU upload failed (exit {status}).")
    print("DONE. XIAO flashed and rebooting into the radio firmware.")


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

    ensure_pyserial()
    print(">> looking for XIAO bootloader drive ...")
    drive = find_uf2_drive()
    before = drive
    port = None
    if not drive:
        touch_1200()
        port_since = None
        for _ in range(30):
            drive = find_uf2_drive()
            if drive:
                break
            port = bootloader_port()
            if port:
                # A bootloader that mounts a drive does it within a few seconds of its port.
                # After that, it is a bootloader with only a serial port.
                port_since = port_since or time.monotonic()
                if time.monotonic() - port_since >= DRIVE_GRACE_S:
                    break
            time.sleep(1)
    if drive and drive == before:
        # The letter was there before we touched anything, so it can belong to a different
        # board. Check what the drive reports. Do not trust the letter.
        print(f"   note: {drive} was already mounted before the touch")
    if not drive:
        port = port or bootloader_port()
        if port:
            flash_serial_dfu(port, uf2_given=bool(args))
            return
        sys.exit(
            "ERROR: no XIAO found: no UF2 bootloader drive, and no XIAO serial port.\n"
            "Connect the USB-C port of the XIAO. If it is connected, double-tap its reset\n"
            "button, then run this again."
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
