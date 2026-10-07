# MeshTerm on the uConsole

A ClockworkPi uConsole with the hackergadgets AIO LoRa board has no companion
microcontroller and no firmware to flash. The LoRa chip is directly on the SPI bus of the
host. MeshTerm can drive the chip directly. When you connect, MeshTerm starts a mesh node
as a child process. It talks to the node over the standard companion protocol on a loopback
port. When you disconnect or quit, MeshTerm ends the node. There is no separate service and
no TCP port to remember. You only run `meshterm --spi`.

If you want the node to stay on the mesh while MeshTerm is closed, you can use a small
bridge. The bridge runs the node as a background service. It presents the same chip as a
network companion. The bridge is [further down](#always-on-the-bridge). Most users must
use the direct connection that is described before it.

> ⚠ **WARNING: Read this first.** Each route edits the boot configuration of your
> uConsole, adds your user to hardware groups, and then keys a LoRa transmitter. You must
> know a Linux shell well. You must also know which frequency plan and which power limits
> apply where you live. What the radio does on the air is your responsibility, not the
> responsibility of MeshTerm. If a step goes wrong, the system, the radio, or the mesh
> around you can be in a worse state. You do this at your own risk.
>
> **Read the whole manual one time before you start.** The install route that you take
> depends on how you run MeshTerm. You can choose the correct route more easily when you
> know the later steps.
>
> This is a guide, not a prescription. The author tried to make it correct, but it can
> have errors. Parts of it will become out of date when packages, runtimes, and boards
> change. You must check each fact in it against your own hardware and the current sources
> before you act on it. Each decision is yours. The author accepts no responsibility for
> damage, loss, or injury that results from this guide.

- [What you need](#what-you-need)
- [Step 1: Prepare the system](#step-1-prepare-the-system)
- [Step 2: Install the radio library](#step-2-install-the-radio-library)
- [Step 3: Install MeshTerm](#step-3-install-meshterm)
- [Step 4: Connect](#step-4-connect)
- [Moving over from the bridge (0.9.0 and earlier)](#moving-over-from-the-bridge-090-and-earlier)
- [Board wiring](#board-wiring)
  - [Which GPIO chip](#which-gpio-chip)
- [Troubleshooting](#troubleshooting)
- [Always on: the bridge](#always-on-the-bridge)
- [What this has been tested on](#what-this-has-been-tested-on)

---

## What you need

**Hardware:**

| Part | Notes |
| --- | --- |
| ClockworkPi **uConsole** with a Raspberry Pi **CM4 or CM5** | Both cores work in the same way. MeshTerm finds the GPIO chip behind the header on each core (refer to [Which GPIO chip](#which-gpio-chip)). ClockworkPi's A-04, A-06 and R-01 cores are not supported. |
| hackergadgets **AIO** expansion board | This guide is written for this board, and we recommend it. It has an SX1262 on SPI bus 1. It also has a GPS, an RTL-SDR, and a USB hub that you do not need here. MeshTerm's SPI defaults are the wiring of this board (AIO v1). The v2 needs one more setting (refer to [Board wiring](#board-wiring)). |
| An antenna for the LoRa band of your region | Necessary. |

This guide does not describe how to install the AIO board in the uConsole. For that part,
follow
[the setup guide of hackergadgets](https://hackergadgets.com/pages/hackergadgets-uconsole-rtl-sdr-lora-gps-rtc-usb-hub-all-in-one-extension-board-setup-guide).

**Software:**

- Raspberry Pi OS / Debian **bookworm** or **trixie** on the uConsole, 64-bit. This is for
  Linux only. Windows and macOS cannot drive a radio on the SPI bus of the host.
- A `/dev/spidev*` device file. This needs `dtoverlay=spi1-1cs` in
  `/boot/firmware/config.txt`.
- The GPIO character device files (`/dev/gpiochip*`). You must also be a member of the
  `spi` and `gpio` groups, so that you can read and write both without root.
- The radio library, **`openhop-core[hardware]` 1.1.3 or newer**. [Step
  2](#step-2-install-the-radio-library) tells you how to get it. It does not have to be
  in MeshTerm's own Python. MeshTerm looks for it in several places.
- MeshTerm itself, on the uConsole ([Step 3](#step-3-install-meshterm)).

The radio settings of a node are not part of this setup. These settings are the frequency,
the bandwidth, the spreading factor, the coding rate, and the TX power. The node owns them
and saves them. You change them in the same way as on any companion, on the Device config
page of MeshTerm. A new node starts with the US/Canada preset of MeshCore (910.525 MHz,
62.5 kHz, SF 7, CR 4/5, 22 dBm) until you change it.

---

## Step 1: Prepare the system

1. Enable the SPI overlay. Edit `/boot/firmware/config.txt` and add this line:

```
dtoverlay=spi1-1cs
```

2. Add yourself to the groups that own the SPI and GPIO device files:

```bash
sudo usermod -aG spi,gpio $USER
```

3. Reboot. The overlay and the group membership take effect after the reboot:

```bash
sudo reboot
```

4. After the uConsole starts again, check that the device files exist and that you can use
   them:

```bash
ls -l /dev/spidev* /dev/gpiochip*
groups
```

You must see `/dev/spidev1.0` (or another `spidev*` file) and at least one `gpiochip` in
the list. You must also see `spi` and `gpio` in the output of `groups`. The `gpiochip` that
has the pins of the radio depends on your core and your kernel. MeshTerm finds it itself
(refer to [Which GPIO chip](#which-gpio-chip)).

---

## Step 2: Install the radio library

MeshTerm needs `openhop-core[hardware]` 1.1.3 or newer. Some Python on the machine must be
able to import it. It does not have to be MeshTerm's own Python. MeshTerm looks in
this order:

1. A `python` that is named in the `spi` table of the profile.
2. MeshTerm's own interpreter.
3. The venv of the bridge, at `~/.local/share/meshterm-spi-bridge/venv/bin/python`.
4. `/opt/venvs/meshcore-uconsole/bin/python`.
5. `python3` on your `PATH`.

Whichever route you take below, the library goes into one of these interpreters. Select
the route that agrees with how you installed MeshTerm.

- **Did you install MeshTerm with `pipx`?** Inject the library into MeshTerm's own environment:

  ```bash
  pipx inject mesh-term 'openhop-core[hardware]'
  ```

- **Did you install MeshTerm with `pip`?** Add the `spi` extra when you install MeshTerm
  (refer to [Step 3](#step-3-install-meshterm)):

  ```bash
  pip install 'mesh-term[spi]'
  ```

- **Do you run the one-file Linux ARM64 build?** It is a single frozen binary, and it
  cannot hold the library. Thus give it a venv of its own. This is the same venv that the
  bridge guide uses, so you can share it if you run both:

  ```bash
  python3 -m venv ~/.local/share/meshterm-spi-bridge/venv
  ~/.local/share/meshterm-spi-bridge/venv/bin/pip install "openhop-core[hardware]"
  ```

  MeshTerm finds the venv there automatically. It needs no configuration.

A direct connection does not support the older `pymc_core` runtime (the name of the library
before 2026). Only the standalone bridge still has the compatibility shims that this
runtime needs.

---

## Step 3: Install MeshTerm

The installation is the same as on any other machine. The simplest way is the Linux ARM64
one-file release:

```bash
curl -fL -o meshterm https://github.com/jpmartineau/MeshTerm/releases/latest/download/meshterm-linux-arm64
chmod +x meshterm
sudo mv meshterm /usr/local/bin/meshterm
```

You can also install it from the repository with `pipx`:

```bash
sudo apt install pipx
pipx install git+https://github.com/jpmartineau/MeshTerm
pipx ensurepath
pipx inject mesh-term 'openhop-core[hardware]'
```

Or use `pip`, which adds the radio library as its own extra in one line:

```bash
pip install 'mesh-term[spi]'
```

---

## Step 4: Connect

```bash
meshterm --spi
```

`--spi` finds the radio of the AIO at `/dev/spidev1.0` and starts a node on it. The startup
device picker also lists the radio, when that device file exists. The row reads **uConsole
AIO (/dev/spidev1.0)** and has a 📍 icon. Thus `meshterm` with no flags also gets you there.

MeshTerm uses that name only when four things show that it is on a uConsole:

- the screen of the uConsole, as the overlay of ClockworkPi for the uConsole declares it
- the power chip, as the same overlay declares it
- the backlight, as the same overlay declares it
- the uConsole's own keyboard, on USB

If one of these is missing, the row reads **SPI radio**.

You are connected when **Device info** or the dashboard shows a node name and not a
connection error. MeshTerm shows the device model as **uConsole AIO**. If it is not sure
that it is on a uConsole, it shows **MeshTerm SPI node**. The connection label in the
header reads **SPI**.

**What happens underneath.** MeshTerm starts the node when it connects. It ends the node
when you disconnect or quit. This includes a crash: the node exits with MeshTerm, and the
kernel frees the GPIO lines and the SPI handle when the process ends. The result is that
**the node is off the mesh while MeshTerm is closed.** If you want the node on the mesh
all the time, use [the bridge](#always-on-the-bridge).

**Where the state is.** Each radio keeps its identity, preferences, channels, and contacts
in `~/.meshterm/radio/spidev1.0/`. The files are `identity.key`, `prefs.json`,
`channels.json`, and `contacts.json`. The folder also has a running log, `node.log`.
MeshTerm keeps the log of the previous run as `node.log.1`. All of these files stay after
a restart.

**A profile, if you want a name for the connection:**

```toml
[profiles.aio]
transport = "spi"
```

```bash
meshterm -p aio
```

The AIO's wiring is the default. Thus this profile needs only `transport = "spi"`.
Read [Board wiring](#board-wiring) if the wiring of your board is different.

---

## Moving over from the bridge (0.9.0 and earlier)

> **Read this section only if you set up the bridge with MeshTerm 0.9.0 or earlier.** Before
> 0.10.0, the bridge was the only way to use the uConsole's radio. Thus an older setup
> almost always runs a bridge. On a new install of 0.10.0 or newer, there is nothing to move
> over. Go to [Board wiring](#board-wiring).

Did you upgrade from 0.9.0 or earlier, and is the bridge still installed? The first time
that you connect with `--spi` (or with a profile, or with the picker row), MeshTerm carries
the bridge's node over. MeshTerm **copies** the data and never moves it. Thus the
bridge still works if you go back to it. MeshTerm copies these items:

- The identity key. MeshTerm looks first for the copy of the `meshcore-console` GUI
  (`~/.local/share/meshcore-uconsole/identity.key`), as the bridge itself does. If it is
  not there, MeshTerm uses the bridge's own key
  (`~/.local/share/meshterm-spi-bridge/identity.key`).
- The bridge's `contacts.json`.
- The name `uConsole`.
- The bridge's radio settings: the frequency, the bandwidth, the spreading factor, the
  coding rate, and the TX power. MeshTerm reads them from the `MESHCORE_*` environment of
  the bridge service.

This happens only one time, while `~/.meshterm/radio/spidev1.0/` has no `identity.key`.
Thus your node keeps its public key, and it keeps its place in the contact list of each
other node.

**MeshTerm does not copy the wiring.** Your board can need `MESHCORE_EN_PINS` or another
wiring variable for the bridge. If it does, put the equivalent in the `spi` table of the
profile (refer to [Board wiring](#board-wiring)). The direct connection reads the wiring
from there, and never from the bridge's environment.

Only one program can hold the radio at a time. Thus stop the bridge before you switch over:

```bash
systemctl --user stop meshterm-spi-bridge
systemctl --user disable meshterm-spi-bridge   # keep it stopped across reboots
```

If MeshTerm sees that the bridge service runs, it refuses to start the node. It then shows
you the same two commands.

---

## Board wiring

The defaults agree with the hackergadgets uConsole AIO **v1**. This board needs no `spi`
table. State only what is different, under `[profiles.<name>.spi]`. Your board can be
something other than an AIO, for example a LoRa HAT on a Raspberry Pi. In that case,
[Adding a radio on the SPI
bus](../guide/configuration.md#adding-a-radio-on-the-spi-bus) shows a whole profile. It
also shows how to read the pins from the `meshtasticd` configuration of the board:

```toml
[profiles.aio2]
transport = "spi"

[profiles.aio2.spi]
en_pins = [27]   # the AIO v2 needs this
```

| Setting | Default | What it is |
| --- | --- | --- |
| `bus_id`, `cs_id` | `1`, `0` | The SPI bus and the chip select of the radio (`/dev/spidev<bus_id>.<cs_id>`). |
| `cs_pin` | `-1` | A GPIO that MeshTerm drives by hand as the chip select, instead of the chip select of the bus. |
| `gpio_chip` | `-1` | The `/dev/gpiochip<n>` that has the pins below. `-1` finds the chip behind the 40-pin header by its label (refer to [Which GPIO chip](#which-gpio-chip)). |
| `use_gpiod_backend` | `false` | Drive the pins through `gpiod` instead of `python-periphery`. |
| `reset_pin`, `busy_pin`, `irq_pin` | `25`, `24`, `26` | The reset line, the busy line, and the interrupt line (DIO1) of the chip. |
| `txen_pin`, `rxen_pin` | `-1`, `-1` | The transmit-enable and receive-enable lines of an external RF switch, if the board has one. |
| `en_pins` | `[]` | The power-enable lines that go high before MeshTerm touches the chip. The **AIO v2 needs `[27]`.** |
| `leds` | `[]` | Switches that the device tree of the board shows as LEDs (under `/sys/class/leds/`). Write each one as `"name=brightness"`. MeshTerm sets them before it touches the chip. It puts them back as they were when it releases the radio. The Cardputer Zero powers its expansion header in this way. Its Cap needs no profile, but this is what you would write: `["ext_5v_out=1", "ext_usb_gpio_fun=0"]`. |
| `pi4io_bus`, `pi4io_address`, `pi4io_high` | `-1`, `0x43`, `[]` | A PI4IOE5V6408 I/O expander that switches the antenna path of the radio. These are its I2C bus (`-1` for none), its address, and the pins that go high. |
| `use_dio2_rf`, `use_dio3_tcxo` | `true`, `true` | If DIO2 drives the RF switch, and if DIO3 drives a TCXO (both are on for the AIO). |
| `is_waveshare` | `false` | The wiring quirks of the Waveshare HAT, which the radio library knows. |
| `python` | `""` (empty) | An interpreter to run the node under. Use it when the interpreter that MeshTerm finds is not the one that you want. If it is empty, MeshTerm looks for one. |
| `gps_port`, `gps_baud` | `""`, `9600` | A GPS receiver on the same board: its serial port (empty for none) and its speed. The node runs the GPS as MeshCore firmware runs the GPS of a board. You switch it on with the **GPS** row on Device config. The device screen then stops listing the port as a companion. The Cap of the Cardputer Zero needs no profile, but this is what you would write: `"/dev/serial0"`, `115200`. |

If a key is unknown or has the wrong type, MeshTerm stops at startup and names the profile.
MeshTerm never ignores a misspelt pin without a message.

The radio settings are **not** in this table. These are the frequency, the bandwidth, the
spreading factor, the coding rate, and the TX power. The node owns them. You change them on
Device config, as [What you need](#what-you-need) says.

### Which GPIO chip

The reset line, the busy line, and the interrupt line of the AIO are GPIO 25, 24, and 26 on
each core. The pin numbers do not change. What changes is the `/dev/gpiochip<n>` that Linux
puts them on:

| Core | The GPIO controller of the header | Its chip number |
| --- | --- | --- |
| CM4 | `pinctrl-bcm2711` | `0` |
| CM5, early kernels | `pinctrl-rp1` | `4` |
| CM5, kernels since Raspberry Pi made the RP1 an alias of chip 0 ([raspberrypi/linux#6144](https://github.com/raspberrypi/linux/pull/6144)) | `pinctrl-rp1` | `0` |
| CM5, some later kernels | `pinctrl-rp1` | another number, because the load order of the drivers moved it |

Thus a chip number is not a fixed fact about the board. **MeshTerm looks for the chip by its
label.** If `gpio_chip` has its default of `-1`, MeshTerm asks each `/dev/gpiochip<n>` for
its label. It uses the chip that is called `pinctrl-rp1`, `pinctrl-bcm2711`, or
`pinctrl-bcm2835`. Thus the same profile works on a CM4, on a CM5, and after kernel
updates. If no chip has one of these labels, MeshTerm uses chip `0`.

To see what chips your system has, run `gpiodetect` (from the `gpiod` package). It lists
each chip with its label:

```
gpiochip0 [pinctrl-rp1] (54 lines)
gpiochip10 [gpio-brcmstb@107d508500] (32 lines)
…
```

Set `gpio_chip` to a number only to replace the lookup. For example, do this on a board
whose header controller has some other label. The [bridge](#always-on-the-bridge) does not
look for chips by label. On a CM5, set `MESHCORE_GPIO_CHIP` to the number that `gpiodetect`
shows for `pinctrl-rp1`, if that number is not `0`.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| MeshTerm does not find the radio library | Follow [Step 2](#step-2-install-the-radio-library) for the way that you installed MeshTerm. Or set `python` in the `spi` table of the profile to an interpreter that has `openhop-core[hardware]`. |
| No `/dev/spidev1.0` | Add `dtoverlay=spi1-1cs` to `/boot/firmware/config.txt` and reboot. |
| Permission denied when MeshTerm opens the SPI or GPIO device | Run `sudo usermod -aG spi,gpio $USER`, then log out and log in again. |
| "the meshterm-spi-bridge service has the radio" | Stop it with `systemctl --user stop meshterm-spi-bridge`. Also `disable` it to keep it stopped. |
| "meshcore-console has the radio open" | Close the GUI and connect again. |
| Some other program has the radio | The message names the chip. Run `sudo lsof /dev/gpiochip<n>` on it to see which program has it. Only one program can hold the radio at a time. |
| The node starts but never answers | Examine its log: `~/.meshterm/radio/spidev1.0/node.log`. |
| An AIO v2 radio never starts | It needs `en_pins = [27]` in the `spi` table of the profile (refer to [Board wiring](#board-wiring)). |

---

## Always on: the bridge

Most users must use the direct connection that is described above. Use the bridge if you
want the node to be on the mesh **while MeshTerm is closed**. The bridge runs as a
`systemd --user` service, on its own. It does not need MeshTerm to be installed.

The bridge has a disadvantage that the direct connection does not have. When the bridge
runs, it holds the pins of the radio all the time. No other LoRa program can use them until
you stop the bridge. This includes a direct `meshterm --spi` connection.

The bridge puts the standard MeshCore companion protocol in front of the same chip on the
SPI bus, on a local TCP port. MeshTerm then connects to that port as it connects to any
network companion.

- [Bridge step 1: Install the radio runtime](#bridge-step-1-install-the-radio-runtime)
- [Bridge step 2: Get the bridge](#bridge-step-2-get-the-bridge)
- [Bridge step 3: Run the preflight](#bridge-step-3-run-the-preflight)
- [Bridge step 4: Run it once in the foreground](#bridge-step-4-run-it-once-in-the-foreground)
- [Bridge step 5: Install it as a service](#bridge-step-5-install-it-as-a-service)
- [Bridge step 6: Connect MeshTerm to it](#bridge-step-6-connect-meshterm-to-it)
- [Updating and uninstalling the bridge](#updating-and-uninstalling-the-bridge)
- [Bridge hardware knobs](#bridge-hardware-knobs)
- [Bridge troubleshooting](#bridge-troubleshooting)

The wiring and radio defaults of the bridge are the same AIO v1 numbers as the defaults of
the direct connection (refer to the table in [Board wiring](#board-wiring)). But you set
them with `MESHCORE_*` environment variables, not with the `spi` table of a profile.

In 2026, the project renamed the radio library that drives the chip: `pymc_core` became
`openhop_core`. The `pymc-core` package on PyPI stopped at 1.0.12. `openhop-core` continues
from 1.1.x. The bridge supports both libraries. When both are installed, it prefers the
newer one.

### Bridge step 1: Install the radio runtime

The route that you take depends on your Debian release:

- **trixie**, with the `meshcore-uconsole` package (by cwill747) installed: you already
  have a working runtime (based on Python 3.13). Go to
  [step 2](#bridge-step-2-get-the-bridge).
- **bookworm**: the trixie `.deb` of `meshcore-uconsole` 1.12.0 is built on Python 3.13
  and needs glibc 2.38. Thus it does not install on bookworm, and the APT repository has
  nothing for bookworm. The GitHub Releases of the upstream project also have a separate
  bookworm `.deb`, built on Python 3.11. It was not tested with the bridge. The documented
  route is to give the bridge a runtime of its own:

  ```bash
  python3 -m venv ~/.local/share/meshterm-spi-bridge/venv
  ~/.local/share/meshterm-spi-bridge/venv/bin/pip install "openhop-core[hardware]"
  ```

Use this route also if you do not want to install the `meshcore-uconsole` package.

In [step 3](#bridge-step-3-run-the-preflight), you can confirm which runtime the bridge
found. The first check line names the interpreter and the runtime.

### Bridge step 2: Get the bridge

`scripts/uconsole/meshterm-spi-bridge` is one self-contained Python file. It has no install
step of its own. Get it in one of two ways.

Clone the MeshTerm repository:

```bash
git clone https://github.com/jpmartineau/MeshTerm
cd MeshTerm/scripts/uconsole
```

Or download only the one file:

```bash
curl -L -o meshterm-spi-bridge \
  https://raw.githubusercontent.com/jpmartineau/MeshTerm/main/scripts/uconsole/meshterm-spi-bridge
```

Then make the file executable:

```bash
chmod +x meshterm-spi-bridge
```

### Bridge step 3: Run the preflight

```bash
./meshterm-spi-bridge --check
```

This command prints a `Prerequisite check:` block with one line for each check. The table
tells you what each line means and what to do if the check fails:

| Check | Meaning if it fails | Fix |
| --- | --- | --- |
| `node runtime` | None of the interpreters that the bridge tried can import `openhop_core` or `pymc_core`. | Build the venv from [step 1](#bridge-step-1-install-the-radio-runtime). Or set `MESHTERM_PYMC_PYTHON` to a python that has one of them. |
| `SPI device` | There is no `/dev/spidev*` file, or you cannot read and write it. | If there is no file, add `dtoverlay=spi1-1cs` to `/boot/firmware/config.txt` and reboot. If the file exists but you have no permission, run `sudo usermod -aG spi $USER`, then log out and log in again. |
| `GPIO chip` | `/dev/gpiochip0` (or the chip that `MESHCORE_GPIO_CHIP` names) is missing, or you cannot read and write it. | Run `sudo usermod -aG gpio $USER`, then log out and log in again. On a CM5, check [which chip](#which-gpio-chip) has the header. |
| `bridge service` / `radio in use` / `port 5000 busy` / `radio is free` | These lines tell you if a program already holds the radio. It can be the boot service of the bridge, a running `meshcore-console`, or another program that listens on the TCP port. | Only one program can use the radio at a time (refer to [step 6](#bridge-step-6-connect-meshterm-to-it)). If `meshcore-console` runs, close it before you run the bridge. |

If the first three checks pass, you are ready to run the bridge.

### Bridge step 4: Run it once in the foreground

```bash
./meshterm-spi-bridge --run
```

This command runs the preflight again and prints it. Before it starts the bridge, it asks
if it must add a `bridge` profile to your MeshTerm config
(`` Add a 'bridge' profile to /home/you/.meshterm/config.toml so you can just run `meshterm`? [y/N] ``).
You can give either answer. If you answer no, step 5 asks again. Then the command starts
the bridge in this terminal. A healthy startup logs lines like these:

```
node runtime openhop_core 1.1.3 (/home/you/.local/share/meshterm-spi-bridge/venv/bin/python)
radio config bus_id=1 reset_pin=25 busy_pin=24 irq_pin=26 frequency=910525000 tx_power=22
radio.begin() attempt 1/4
radio ready
node identity ab12cd34ef567890… (hash 0xab, name 'uConsole')
READY — connect with:  meshterm --tcp 127.0.0.1:5000
```

The `radio config` line lists each setting as `key=value`. The line above is shortened.
Your line is longer.

To test the bridge, leave it running. In another terminal on the same machine, run:

```bash
meshterm --tcp 127.0.0.1:5000
```

Then go to the terminal of the bridge and press **Ctrl+C** to stop it.

### Bridge step 5: Install it as a service

1. Run the bridge with no flags to get the interactive menu:

```bash
./meshterm-spi-bridge
```

2. Choose **2) Install as a service**.

This choice does these things:

- It copies the script to `~/.local/bin/meshterm-spi-bridge`.
- It writes a `systemd --user` unit at
  `~/.config/systemd/user/meshterm-spi-bridge.service`. The unit restarts on a failure and
  starts with your session.
- It enables the unit and starts it now.
- It tries to enable lingering for your user. Then the service starts at boot, before you
  log in. If lingering fails, the script prints a command for you to run:

```bash
sudo loginctl enable-linger $USER
```

The script also offers to add a `[profiles.bridge]` block to your MeshTerm `config.toml`.
Answer yes. You use this profile next.

To check the service at any time, run:

```bash
systemctl --user status meshterm-spi-bridge.service
journalctl --user -u meshterm-spi-bridge.service -f
```

Menu option **4) Show status** prints the same information from inside the script.

### Bridge step 6: Connect MeshTerm to it

Install MeshTerm on the uConsole in the same way as in [Step 3](#step-3-install-meshterm)
above. The bridge runs the node. Thus MeshTerm's own Python does not need the `spi` extra or
`openhop-core[hardware]`.

If you accepted the profile in step 4 or step 5, `~/.meshterm/config.toml` now has this
block. The bridge always writes to that path, also if you set `MESHTERM_HOME`:

```toml
[profiles.bridge]
host = "127.0.0.1"
tcp_port = 5000
description = "Local SPI radio via meshterm-spi-bridge"
```

Connect with:

```bash
meshterm -p bridge
```

Or connect without a profile:

```bash
meshterm --tcp 127.0.0.1:5000
```

You are connected when **Device info** or the dashboard shows a node name (`uConsole` by
default) and not a connection error. MeshTerm shows the device model as `pyMC-spi-bridge`.
With this model name, you can tell a node on the bridge from a real companion or from a
direct SPI connection.

**Identity.** If the package of the `meshcore-console` GUI is present, the bridge loads the
identity key of *that* package. Thus MeshTerm and the GUI have the same public key. The
contacts and the radio settings belong to the bridge:

- The bridge keeps its contacts in `~/.local/share/meshterm-spi-bridge/contacts.json`.
- The bridge keeps its channels in `~/.local/share/meshterm-spi-bridge/channels.json`.
  Both files stay after a restart.
- The bridge builds its radio settings from the `MESHCORE_*` variables and its defaults.
  It never uses the saved settings of the GUI. Set the `MESHCORE_*` variables to match the
  values that the GUI uses.

On an install that has only the library, the bridge makes and keeps its own identity key in
`~/.local/share/meshterm-spi-bridge/identity.key`.

**Only one program can use the radio at a time.** Never run the bridge and the
`meshcore-console` GUI at the same time. The program that starts second fails to open the
radio. The same is true of a direct `meshterm --spi` connection while the bridge runs.

### Updating and uninstalling the bridge

To update the bridge, pull or download `meshterm-spi-bridge` again. Then install the service
again (menu option 2). This overwrites the copy in `~/.local/bin/`. The running service
keeps the old code until you restart it:

```bash
systemctl --user restart meshterm-spi-bridge.service
```

Option 2 also writes the unit file again. Thus it removes each `Environment=` line that you
added to the file by hand. Put these lines in a drop-in instead, as
[Bridge hardware knobs](#bridge-hardware-knobs) shows.

To remove the bridge, run the menu and choose **3) Uninstall the service**. This choice
stops the `systemd --user` unit and removes it. It also removes the installed copy of the
script. It does **not** change `meshcore-console`, the radio library, the identity of your
node, or your saved contacts and channels. These stay as they are. If you added
`[profiles.bridge]` to your MeshTerm config, the script tells you to remove that block by
hand when you no longer want it.

### Bridge hardware knobs

The pin defaults agree with the hackergadgets uConsole AIO v1. This board needs none of
these variables. The **AIO v2 needs `MESHCORE_EN_PINS=27`**, whether or not the
`meshcore-console` package is installed. The radio settings default to the US/Canada
preset. To run the bridge in the foreground, export the variables that are different before
you start it. For the service, add a drop-in. A drop-in stays after a reinstall:

```bash
systemctl --user edit meshterm-spi-bridge.service
```

Put the variables under a `[Service]` heading, with one `Environment=` line for each:

```ini
[Service]
Environment=MESHCORE_EN_PINS=27
```

Then run `systemctl --user restart meshterm-spi-bridge.service`.

| Variable | What it sets |
| --- | --- |
| `MESHCORE_BUS_ID`, `MESHCORE_CS_ID`, `MESHCORE_CS_PIN` | The SPI bus, the SPI device, and the chip-select pin of the radio |
| `MESHCORE_RESET_PIN`, `MESHCORE_BUSY_PIN`, `MESHCORE_IRQ_PIN` | The three control lines |
| `MESHCORE_FREQUENCY`, `MESHCORE_TX_POWER` | The frequency in Hz and the power in dBm |
| `MESHCORE_SPREADING_FACTOR`, `MESHCORE_BANDWIDTH`, `MESHCORE_CODING_RATE` | The modem preset. All three must match the mesh that you join. Use whole numbers only: the bandwidth in Hz (`62500`), and the coding rate as the denominator (`5` for 4/5). A decimal or `4/5` stops the bridge at startup. |
| `MESHCORE_TXEN_PIN`, `MESHCORE_RXEN_PIN`, `MESHCORE_EN_PINS` | The RF-switch lines and the power-enable lines that a board can need (`-1` for none). `EN_PINS` is a comma list. The AIO v2 needs `27`. |
| `MESHCORE_USE_DIO2_RF`, `MESHCORE_USE_DIO3_TCXO`, `MESHCORE_IS_WAVESHARE` | If DIO2 drives the RF switch, and if DIO3 drives the TCXO (both are on for the AIO). The last one is a flag for the wiring of a different vendor that the runtime knows. |
| `MESHCORE_GPIO_CHIP`, `MESHCORE_USE_GPIOD_BACKEND`, `MESHCORE_PREAMBLE_LENGTH` | The gpiochip (`0` unless you set it). The bridge does not look for it by label. On a CM5, refer to [Which GPIO chip](#which-gpio-chip). Then the choice to drive the pins through `gpiod`. Then the LoRa preamble. Leave the preamble unset. The bridge follows MeshCore: 32 symbols up to SF8, and 16 symbols above SF8. A shorter preamble makes the radio deaf to most of the mesh. |
| `MESHTERM_PYMC_PYTHON` | Forces a specific interpreter, so that the bridge does not look for one |

The bridge passes a knob only when the radio constructor of the runtime accepts it. Thus
the same environment works under `pymc_core` 1.0.x, which does not have the newer knobs.
If the `meshcore-console` package is installed, the bridge builds the radio through the
environment reader of that package. This reader reads the same names and a few more of its
own (refer to the documentation of that package). It ignores the saved presets of the GUI.
On an install that has only the library, the table above is complete. In each case, an AIO
v2 without `MESHCORE_EN_PINS=27` does not power its radio.

### Bridge troubleshooting

| Symptom | Check |
| --- | --- |
| `node runtime` fails in the preflight | Did you build the venv in [step 1](#bridge-step-1-install-the-radio-runtime)? If `MESHTERM_PYMC_PYTHON` is set, does it point to the correct interpreter? |
| `SPI device` shows "no /dev/spidev* found" | Is `dtoverlay=spi1-1cs` in `/boot/firmware/config.txt`? Did you reboot after you added it? |
| `SPI device` or `GPIO chip` shows a permission error | Are you in the `spi` and `gpio` groups (`groups`)? Did you log out and log in again after `usermod`? |
| Preflight warns `radio in use         meshcore-console is running:` | Close the GUI before you run or install the bridge. |
| Preflight warns `port 5000 busy         something is already on 127.0.0.1:5000` | A program other than the bridge service listens there. (An active service shows as `bridge service running` instead.) Usually it is a foreground `--run` in another terminal. Stop that one. |
| Bridge exits with `bridge failed: GPIO pins still busy after retries` | The bridge tries `radio.begin()` four times, only when the GPIO lines are busy. A session that just closed can still hold them. Wait a few seconds and try again. |
| MeshTerm connects but direct messages to an old contact say "not found" | The bridge restores contacts from its snapshot file at startup. If the contact is not in that file, wait until the node hears the contact again. |
| A reboot, a factory reset, or a device PIN change does nothing, and there is no message | This is expected. The radio library has no handler for these three actions, with each runtime. |
| Traces or the live feed do not show | These use the compatibility shims that the bridge installs. (Under `openhop_core`, they use the equivalents of the library.) Look in the log of the bridge for lines that start with `compat:`. These lines show that the shims are wired. |

---

## What this has been tested on

**The direct connection** has run on a bookworm uConsole with a **CM5** and an AIO v1
since 2026-09-24, with the defaults of MeshTerm. It was also tested against the real radio
library (`openhop-core`) with a simulated radio. A CM4 was not tried with it. MeshTerm finds
the header chip of a CM4 with the same label lookup (refer to
[Which GPIO chip](#which-gpio-chip)).

**The bridge** was verified on a bookworm uConsole with `openhop-core` 1.1.3. The radio
starts, contacts are restored across a restart, and `info` and `contacts` answer correctly
over the TCP connection. The older `pymc-core` 1.0.x path was tested in full, with all
three compatibility shims of the bridge active. This includes traces and the live feed.
The test suite of MeshTerm does not cover the bridge.

What is not confirmed:

- **The direct connection on a CM4.** Only the CM5 has run it.
- **The bridge under `openhop_core`, beyond `info` and `contacts`.** The tests covered
  startup, identity, contact restore, and those two reads. They did not cover a trace, a
  message, or the live feed under the new runtime. For traces, the bridge now uses the
  trace push of the library itself.
- **Everything on the air.** This includes the trace semantics for each hop that the bridge
  puts together again. It also includes the acknowledgement bytes that newer firmware adds
  after the CRC. The code is consistent with the API of the library. The behaviour on the
  wire was seen on one mesh, with the radios in range of one bench.
