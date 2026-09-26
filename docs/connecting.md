# When a companion won't connect

MeshTerm talks to a MeshCore companion over USB, Bluetooth, or TCP, or runs the node itself
on a radio on the SPI bus. When a connection fails, MeshTerm says which step failed and what
to do about it, in the device picker and on the command line alike. This page collects those
messages, so you can look one up before you meet it, or search for the one you have.

Every failure is also written to the log file, `~/.meshterm/meshterm.log` (see
[Where MeshTerm keeps its state](cli.md#where-meshterm-keeps-its-state)). Attach it to a
bug report. For more detail, set the **Log detail** preference to `DEBUG` (`meshterm
preferences set log_level DEBUG`) and run it again.

- [Bluetooth](#bluetooth)
  - [Who does the pairing](#who-does-the-pairing)
  - [Finding the PIN](#finding-the-pin)
  - [What each message means](#what-each-bluetooth-message-means)
  - [Forgetting a pairing](#forgetting-a-pairing)
- [USB](#usb)
- [TCP](#tcp)
- [A radio on the SPI bus](#a-radio-on-the-spi-bus)

---

## Bluetooth

**A companion takes one Bluetooth connection at a time.** If your phone's MeshCore app is
connected to it, MeshTerm cannot connect until the phone lets go, and the companion stops
advertising while it is connected, so MeshTerm cannot even see it. Disconnect the phone
first, or turn its Bluetooth off.

### Who does the pairing

A PIN-protected companion must be *paired* before it will talk: the computer and the
companion exchange keys once, using the six-digit PIN, and remember them. Who runs that
exchange depends on the system.

| System | Who pairs | What you do |
| --- | --- | --- |
| Windows | MeshTerm | Give the PIN when MeshTerm asks for it (the picker's PIN dialog, or `--ble-pin` on the command line). You do not need to pair in Settings first. |
| Linux | MeshTerm | The same. MeshTerm answers the pairing itself, so it works over SSH and on machines with no desktop, and you do not need `bluetoothctl` or the desktop's Bluetooth settings. |
| macOS | macOS | macOS shows its own dialog asking for the code; type the PIN there. `--ble-pin` has no effect on macOS. |

Once paired, the system remembers it, and later connections need no PIN.

**On macOS, the terminal needs Bluetooth permission.** macOS grants Bluetooth to the app
MeshTerm runs in (Terminal, iTerm2, Ghostty), not to MeshTerm itself. If no pairing dialog
appears, or no Bluetooth devices are listed, check System Settings > Privacy & Security >
Bluetooth. **Over SSH, macOS denies Bluetooth outright** and never asks, so run MeshTerm in
a terminal on the Mac itself.

### Finding the PIN

The PIN is shown on the companion's screen, or in the MeshCore app's settings for it. A
companion with no screen (a T1000-E, for instance) can tell you over USB instead:

```console
$ meshterm --port /dev/ttyACM0 config get device_pin
123456
```

`meshterm config set device_pin <PIN>` changes it, and so does the **Device PIN** row on
the menu's Device config page. A changed PIN makes every existing pairing with that
companion out of date. See [a stale pairing](#what-each-bluetooth-message-means) below.

### What each Bluetooth message means

In the device picker, a pairing problem opens the PIN dialog with a one-line reason under
the question. On the command line, it is the error message. Both come from the same
diagnosis.

| The message starts with… | What happened | What to do |
| --- | --- | --- |
| **couldn't open a Bluetooth link** | The companion was never reached. | Move closer, check it is powered on, and make sure no phone or other computer is connected to it. |
| **requires a Bluetooth pairing PIN** | The companion is PIN-protected and not paired with this computer. | Give the PIN: the picker asks for it; on the command line, add `--ble-pin <PIN>`. |
| **Windows has … paired** / **Linux has … paired, but the device refused that pairing** | Your computer remembers a pairing that the companion no longer accepts. It was reflashed, reset, or given a new PIN, or it was paired *without* a PIN back when it did not have one. | Give the PIN, and MeshTerm removes the old pairing and pairs again. Or [forget the pairing](#forgetting-a-pairing) yourself and reconnect. |
| **rejected the Bluetooth PIN** | The PIN was wrong. | Check the code on the device or in the MeshCore app, and try again. |
| **refused to pair** | The companion turned the pairing down. It is usually connected to a phone, or still holds an old pairing for this computer. | Disconnect it from anything else, or restart it, and try again. |
| **Windows couldn't pair** / **Linux couldn't pair** | The pairing failed for another reason, named in brackets. | Restart the companion, [forget the pairing](#forgetting-a-pairing), and try again. |
| **Windows couldn't reach** / **Linux couldn't reach … to pair it** | The system could not find the companion when it came to pair. | As for a link that never opened: range, power, or another connection. |
| **paired with …, but the device still refuses the connection** | Pairing succeeded, yet the companion still refuses. It is holding an old pairing for this computer. | Restart the companion, [forget the pairing](#forgetting-a-pairing), and try again. |
| **was not paired. macOS asks for the pairing code in its own dialog** | macOS had not finished pairing. | Type the PIN into the macOS dialog when it appears. If none appears, check the terminal's Bluetooth permission (above). |
| **connecting … took longer than** | The link, the pairing, or the service discovery stalled. | Move closer, restart the companion, and try again. |
| **connected …, but it didn't answer the identity query** | The link came up and the companion went quiet. | It may be busy with another app. Restart it and try again. |
| **connected …, but it never answered as a MeshCore companion** | Something answered, but not as a companion. | It may be running repeater or room server firmware, which has no companion interface. |

### Forgetting a pairing

MeshTerm fixes a stale pairing itself when you give it the PIN. To remove one by hand:

- **Windows:** Settings > Bluetooth & devices, open the companion, and choose *Remove device*.
- **Linux:** `bluetoothctl remove AA:BB:CC:DD:EE:FF`, with your companion's address.
- **macOS:** System Settings > Bluetooth, the companion's ⓘ button, then *Forget This Device*.

On Windows and Linux, MeshTerm's quit dialog also offers **Unpair & quit** while a paired
Bluetooth companion is connected. It forgets the pairing on the way out, so the next
connection asks for the PIN again.

---

## USB

| The message starts with… | What happened | What to do |
| --- | --- | --- |
| **no permission to open** | Your user may not open the port. On Linux, serial ports belong to a group, `dialout` on Ubuntu and Debian. | `sudo usermod -aG dialout $USER`, then log out and back in. The new group applies only to a new login. |
| **… is in use by another program** | Something else has the port open: the MeshCore app, a firmware flasher, or a serial monitor. On Windows this is reported as *Access is denied*. | Close the other program. On Linux, ModemManager probes new USB serial devices and can hold one: `sudo systemctl stop ModemManager`, and try again. |
| **… isn't there** | The port does not exist. The device was unplugged, or came back under another name (`/dev/ttyACM0` becoming `/dev/ttyACM1`, `COM5` becoming `COM6`). | `meshterm devices` lists what is attached now. |
| **no response from a MeshCore companion on …** | The port opened, but nothing answered as a companion. | It may not be a MeshCore companion (repeater firmware, or another device), or it is powered off or busy. On Linux, ModemManager may be probing it: `sudo systemctl stop ModemManager`, and try again. |

To stop ModemManager for good rather than for one boot, `sudo systemctl disable --now
ModemManager`, if nothing else on the machine needs it. Nothing does unless you have a
cellular modem.

---

## TCP

**no response from a MeshCore companion at HOST:PORT** means nothing answered there. Check
the host and the port (MeshTerm's default is `5000`), that the companion is powered on and
reachable on your network, and that no other client is already connected to it. Like
Bluetooth, a network companion serves one client at a time.

---

## A radio on the SPI bus

A radio MeshTerm drives itself (`--spi`) is not a companion. It has no pairing and no port,
and its failures are about the node MeshTerm starts. Its own guides cover them:
[the uConsole](uconsole.md) and [adding a radio on the SPI bus](configuration.md#adding-a-radio-on-the-spi-bus).
