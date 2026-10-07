# When a companion does not connect

MeshTerm talks to a MeshCore companion over USB, Bluetooth, or TCP. It can also run the
node itself on a radio on the SPI bus. When a connection fails, MeshTerm tells you which
step failed and what to do. This page lists these messages. Use it to look up a message
before you see it, or to search for the message that you have.

You see the same message wherever the connection fails:

- **In the device picker**, when you select a device.
- **When MeshTerm starts**, if you named the device on the command line (`--port`, `--ble`,
  `--tcp`, or `--profile`). MeshTerm shows the message. Then it opens the menu without a
  radio. A tool that needs the radio tries to connect again when you open it.
- **While MeshTerm waits for a device to come back**, after you unplugged the device or it
  rebooted. If the device comes back but still does not connect, the message shows under
  the spinner. Thus you do not wait for something that will not happen. Correct the
  problem. Then MeshTerm connects, and you do nothing more.
- **On the command line**, as the error message.

MeshTerm also writes each failure to the log file, `~/.meshterm/meshterm.log` (refer to
[Where MeshTerm keeps its state](../cli/README.md#where-meshterm-keeps-its-state)). Attach
the log file to a bug report. To get more detail, set the **Log detail** preference to
`DEBUG` (`meshterm preferences set log_level DEBUG`), and run the command again.

Sometimes MeshTerm meets a failure that it has no name for. Then the message is the error
in its own words, and a second line tells where the log file is. The log has the full
error, which a bug report needs.

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

**A companion takes one Bluetooth connection at a time.** If the MeshCore app on your phone
is connected to the companion, MeshTerm cannot connect until the phone disconnects. While
the companion is connected, it stops its BLE advertisement, so MeshTerm cannot see it.
Disconnect the phone first, or turn its Bluetooth off.

### Who does the pairing

A companion that has a PIN must be *paired* before it will talk. The computer and the
companion exchange keys one time, with the six-digit PIN, and they remember the keys. The
system decides who does this exchange.

| System | Who pairs | What you do |
| --- | --- | --- |
| Windows | MeshTerm | Give the PIN when MeshTerm asks for it (in the PIN dialog of the picker, or with `--ble-pin` on the command line). You do not pair in Settings first. |
| Linux | MeshTerm | Do the same as on Windows. MeshTerm answers the pairing itself. Thus it works over SSH and on machines that have no desktop. You do not need `bluetoothctl` or the Bluetooth settings of the desktop. |
| macOS | macOS | macOS shows its own dialog that asks for the code. Type the PIN there. `--ble-pin` has no effect on macOS. |

After the pairing, the system remembers it. Later connections do not need the PIN.

**On macOS, the terminal needs permission for Bluetooth.** macOS gives Bluetooth permission
to the app that MeshTerm runs in (Terminal, iTerm2, or Ghostty). It does not give it to
MeshTerm. If no pairing dialog appears, or no Bluetooth devices are listed, check System
Settings > Privacy & Security > Bluetooth. **Over SSH, macOS denies Bluetooth completely**
and never asks. Thus you must run MeshTerm in a terminal on the Mac itself.

### Finding the PIN

The PIN is on the screen of the companion, or in the settings for it in the MeshCore app.
If the companion has no screen (a T1000-E, for example), you can get the PIN over USB:

```console
$ meshterm --port /dev/ttyACM0 config get device_pin
123456
```

To change the PIN, run `meshterm config set device_pin <PIN>`. You can also use the
**Device PIN** row on the Device config page of the menu. When you change the PIN, each
existing pairing with that companion is out of date. Refer to
[a stale pairing](#what-each-bluetooth-message-means) below.

### What each Bluetooth message means

In the device picker, a pairing problem opens the PIN dialog. The dialog shows a one-line
reason under the question. On the command line, the reason is the error message. Both come
from the same diagnosis.

| The message starts with… | What happened | What to do |
| --- | --- | --- |
| **"couldn't open a Bluetooth link"** | MeshTerm did not reach the companion. | Move closer. Make sure that the companion is on. Make sure that no phone or other computer is connected to it. |
| **"requires a Bluetooth pairing PIN"** | The companion has a PIN, and it is not paired with this computer. | Give the PIN. The picker asks for it. On the command line, add `--ble-pin <PIN>`. |
| **"Windows has … paired"** / **"Linux has … paired, but the device refused that pairing"** | Your computer remembers a pairing that the companion does not accept now. The companion was reflashed, reset, or given a new PIN. Or you paired it *without* a PIN when it had no PIN. | Give the PIN. MeshTerm removes the old pairing and pairs again. Or [forget the pairing](#forgetting-a-pairing) yourself, and connect again. |
| **"rejected the Bluetooth PIN"** | The PIN was wrong. | Check the code on the device or in the MeshCore app. Then try again. |
| **"refused to pair"** | The companion did not accept the pairing. Usually it is connected to a phone, or it still has an old pairing for this computer. | Disconnect the companion from anything else, or restart it. Then try again. |
| **"Windows couldn't pair"** / **"Linux couldn't pair"** | The pairing failed for another reason. The message names the reason in brackets. | Restart the companion. [Forget the pairing](#forgetting-a-pairing). Then try again. |
| **"Windows couldn't reach"** / **"Linux couldn't reach … to pair it"** | The system could not find the companion when it tried to pair. | Do the same as for a link that did not open: check the range, the power, and other connections. |
| **"paired with …, but the device still refuses the connection"** | The pairing worked, but the companion still refuses the connection. It has an old pairing for this computer. | Restart the companion. [Forget the pairing](#forgetting-a-pairing). Then try again. |
| **"was not paired. macOS asks for the pairing code in its own dialog"** | macOS did not finish the pairing. | Type the PIN in the macOS dialog when it appears. If no dialog appears, check the Bluetooth permission of the terminal (refer to the text above). |
| **"connecting … took longer than"** / **"connecting … timed out"** | The link, the pairing, or the service discovery stopped. | Move closer. Restart the companion. Then try again. |
| **"connected …, but it didn't answer the identity query"** | The link came up, and then the companion did not answer. | Make sure that no other app uses the companion, because it can be busy. Restart it and try again. |
| **"connected …, but it never answered as a MeshCore companion"** | Something answered, but it did not answer as a companion. | Find out if the device has the firmware of a repeater or a room server. These have no companion interface. |

### Forgetting a pairing

When you give MeshTerm the PIN, it repairs a stale pairing itself. To remove a pairing by
hand, do the step for your system:

- **Windows:** Go to Settings > Bluetooth & devices. Open the companion, and select *Remove
  device*.
- **Linux:** Run `bluetoothctl remove AA:BB:CC:DD:EE:FF`. Use the address of your companion.
- **macOS:** Go to System Settings > Bluetooth. Select the ⓘ button of the companion, then
  *Forget This Device*.

On Windows and Linux, the quit dialog of MeshTerm also has **Unpair & quit** while a paired
Bluetooth companion is connected. This choice forgets the pairing as MeshTerm leaves. Thus
the next connection asks for the PIN again.

---

## USB

| The message starts with… | What happened | What to do |
| --- | --- | --- |
| **"no permission to open"** | Your user may not open the port. On Linux, a group owns the serial ports. It is `dialout` on Ubuntu and Debian. | Run `sudo usermod -aG dialout $USER`. Then log out and log in again. The new group applies only to a new login. |
| **"… is in use by another program"** | Another program has the port open: the MeshCore app, a firmware flasher, or a serial monitor. On Windows, the system reports this as *Access is denied*. | Close the other program. On Linux, ModemManager probes new USB serial devices, and it can hold one. Run `sudo systemctl stop ModemManager`, and try again. |
| **"… isn't there"** | The port does not exist. The device was unplugged, or it came back under another name (`/dev/ttyACM0` became `/dev/ttyACM1`, or `COM5` became `COM6`). | Run `meshterm devices` to list what is attached now. |
| **"no response from a MeshCore companion on …"** | The port opened, but nothing answered as a companion. | Make sure that the device is a MeshCore companion. It can have the firmware of a repeater, or it can be another device. It can also be off or busy. On Linux, ModemManager can be probing it. Run `sudo systemctl stop ModemManager`, and try again. |

To stop ModemManager for good, and not only for one boot, run `sudo systemctl disable --now
ModemManager`. Do this only if nothing else on the machine needs ModemManager. Nothing
needs it unless you have a cellular modem.

---

## TCP

A network companion serves one client at a time, as a Bluetooth companion does. The default
port of MeshTerm is `5000`.

| The message starts with… | What happened | What to do |
| --- | --- | --- |
| **"couldn't find a host named"** | The name did not lead to an address. | Check the spelling, or use the IP address of the companion. |
| **"nothing is accepting connections on port"** | MeshTerm reached the computer, but nothing listens on that port. | Check the port number. Make sure that the companion, or the bridge in front of it, is running. |
| **"there's no route to"** | Your computer cannot reach that address. | Check the address. Make sure that your computer is on the network of the companion (or on its VPN). |
| **"no answer from … on port"** | Nothing answered in time. | Make sure that the companion is on and is on your network. A firewall can hold a connection without a refusal. |
| **"… closed the connection as it opened"** | The companion hung up at once. | Disconnect the other client first. Probably another client is connected to the companion. |
| **"the connection to … opened, but nothing answered as a MeshCore companion"** | Something listens on the port, but it is not a companion. | Check the port number. If the number is correct, the companion can be busy with another client. |

---

## A radio on the SPI bus

A radio that MeshTerm drives itself (`--spi`) is not a companion. It has no pairing and no
port. Its failures are about the node that MeshTerm starts. Its own guides cover these
failures: [the uConsole](../devices/uconsole.md) and
[adding a radio on the SPI bus](configuration.md#adding-a-radio-on-the-spi-bus).
