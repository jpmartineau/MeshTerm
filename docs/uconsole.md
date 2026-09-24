# MeshTerm on the uConsole

A ClockworkPi uConsole with the hackergadgets AIO LoRa board has no companion
microcontroller and no firmware to flash — the LoRa chip sits straight on the host's SPI
bus. MeshTerm can drive it directly: it starts a mesh node as a child process the moment
you connect, talks to it over the standard companion protocol on a loopback port, and ends
it when you disconnect or quit. No separate service, no TCP port to remember — just
`meshterm --spi`.

If you'd rather the node stayed on the mesh while MeshTerm is closed, a small bridge can
run it as a background service instead, fronting the same chip as a network companion.
That's [further down](#always-on-the-bridge) — most people want the section above it.

> ⚠ **Read this first.** Either route edits your uConsole's boot configuration, adds your
> user to hardware groups, and then keys a LoRa transmitter. It assumes you are comfortable
> at a Linux shell and know which frequency plan and power limits apply where you live —
> what the radio does on air is your responsibility, not MeshTerm's. There is a real risk
> of leaving the system, the radio or the mesh around you in a worse state if a step goes
> wrong. You do this at your own risk.
>
> **Read the whole manual through once before you start.** Which install route you take
> depends on how you're running MeshTerm, and that's easier to get right knowing the later
> steps.
>
> This is a guide, not a prescription. Despite the author's best efforts it may contain
> errors, and parts of it will go out of date as packages, runtimes and boards change. It
> is up to you to check every fact in it against your own hardware and the current sources
> before you act on it, and every decision along the way is yours. The author accepts no
> responsibility for any damage, loss or injury that results from following this guide.

- [What you need](#what-you-need)
- [Step 1 — Prepare the system](#step-1--prepare-the-system)
- [Step 2 — Install the radio library](#step-2--install-the-radio-library)
- [Step 3 — Install MeshTerm](#step-3--install-meshterm)
- [Step 4 — Connect](#step-4--connect)
- [Moving over from the bridge](#moving-over-from-the-bridge)
- [Board wiring](#board-wiring)
- [Troubleshooting](#troubleshooting)
- [Always on: the bridge](#always-on-the-bridge)
- [What this has been tested on](#what-this-has-been-tested-on)

---

## What you need

**Hardware:**

| Part | Notes |
| --- | --- |
| ClockworkPi **uConsole** (CM4 module) | Any uConsole with a Compute Module 4. |
| hackergadgets **AIO** expansion board | The board this guide is written for and the one we recommend: an SX1262 on SPI bus 1, plus GPS, an RTL-SDR and a USB hub you don't need here. MeshTerm's SPI defaults are its wiring (AIO v1; the v2 needs one extra setting — see [Board wiring](#board-wiring)). |
| An antenna for your region's LoRa band | Required. |

Installing the AIO board into the uConsole is out of scope here — follow
[hackergadgets' own setup guide](https://hackergadgets.com/pages/hackergadgets-uconsole-rtl-sdr-lora-gps-rtc-usb-hub-all-in-one-extension-board-setup-guide)
for that part.

**Software:**

- Raspberry Pi OS / Debian **bookworm** or **trixie** on the uConsole, 64-bit. This is
  Linux-only — driving a radio on the host's own SPI bus is not something Windows or macOS
  can do.
- A `/dev/spidev*` node. This needs `dtoverlay=spi1-1cs` in `/boot/firmware/config.txt`.
- `/dev/gpiochip0`, and membership of the `spi` and `gpio` groups so you can read and write
  both without root.
- The radio library, **`openhop-core[hardware]` 1.1.3 or newer** — [Step
  2](#step-2--install-the-radio-library) gets you there. It doesn't have to be in
  MeshTerm's own Python; MeshTerm looks in several places for it.
- MeshTerm itself, on the uConsole — [Step 3](#step-3--install-meshterm).

A node's own radio settings — frequency, bandwidth, spreading factor, coding rate, TX
power — are not part of this setup. They're the node's, saved by the node, and you change
them the same way you would on any companion: MeshTerm's Device config page. A brand-new
node starts on MeshCore's US/Canada preset (910.525 MHz, 62.5 kHz, SF 7, CR 4/5, 22 dBm)
until you change it.

---

## Step 1 — Prepare the system

Enable the SPI overlay. Edit `/boot/firmware/config.txt` and add:

```
dtoverlay=spi1-1cs
```

Add yourself to the groups that own the SPI and GPIO device nodes:

```bash
sudo usermod -aG spi,gpio $USER
```

Reboot so both the overlay and the group membership take effect:

```bash
sudo reboot
```

After it comes back, check the device nodes exist and that you can use them:

```bash
ls -l /dev/spidev* /dev/gpiochip0
groups
```

You should see `/dev/spidev1.0` (or another `spidev*` node) and `/dev/gpiochip0` listed,
and `spi` and `gpio` in your `groups` output.

---

## Step 2 — Install the radio library

MeshTerm needs `openhop-core[hardware]` 1.1.3 or newer importable by *some* Python on the
machine — not necessarily MeshTerm's own. It looks, in order: a `python` named in the
profile's `spi` table; MeshTerm's own interpreter; the bridge's venv at
`~/.local/share/meshterm-spi-bridge/venv/bin/python`; `/opt/venvs/meshcore-uconsole/bin/python`;
then `python3` on your `PATH`. Whichever route you take below, that's the interpreter it
lands in.

- **Installed MeshTerm with `pipx`?** Inject it into MeshTerm's own environment:

  ```bash
  pipx inject mesh-term 'openhop-core[hardware]'
  ```

- **Installed with `pip`?** Pull in the `spi` extra when you install MeshTerm (see
  [Step 3](#step-3--install-meshterm)):

  ```bash
  pip install 'mesh-term[spi]'
  ```

- **Running the one-file Linux ARM64 build?** It's a single frozen binary and can't carry
  the library, so give it a venv of its own — the same one the bridge guide uses, so it's
  shared if you ever run both:

  ```bash
  python3 -m venv ~/.local/share/meshterm-spi-bridge/venv
  ~/.local/share/meshterm-spi-bridge/venv/bin/pip install "openhop-core[hardware]"
  ```

  MeshTerm finds it there automatically; no configuration needed.

The older `pymc_core` runtime (the library's name before 2026) is not supported for a
direct connection — only the standalone bridge still carries the compatibility shims it
needs.

---

## Step 3 — Install MeshTerm

Same as any other machine — the simplest way is the Linux ARM64 one-file release:

```bash
curl -fL -o meshterm https://github.com/jpmartineau/MeshTerm/releases/latest/download/meshterm-linux-arm64
chmod +x meshterm
sudo mv meshterm /usr/local/bin/meshterm
```

Or install it from the repository with `pipx`:

```bash
sudo apt install pipx
pipx install git+https://github.com/jpmartineau/MeshTerm
pipx ensurepath
pipx inject mesh-term 'openhop-core[hardware]'
```

Or with `pip`, pulling in the radio library as its own extra in one line:

```bash
pip install 'mesh-term[spi]'
```

---

## Step 4 — Connect

```bash
meshterm --spi
```

`--spi` finds the AIO's radio at `/dev/spidev1.0` and starts a node on it. The startup
device picker also lists it — a row reading **SPI radio (/dev/spidev1.0)** with a 📍 icon —
whenever that device node exists, so `meshterm` with no flags gets you there too.

You're connected when **Device info** or the dashboard shows a node name instead of a
connection error. MeshTerm reports the device model as **MeshTerm SPI node**, and the
header's connection label reads **SPI**.

**What happens underneath.** MeshTerm starts the node when it connects and ends it when
you disconnect or quit — including a crash: the node exits with MeshTerm, and the kernel
frees the GPIO lines and SPI handle the moment its process does. The trade-off is the one
this implies: **the node is off the mesh while MeshTerm is closed.** If you want it on all
the time, that's what [the bridge](#always-on-the-bridge) is for.

**Where its state lives.** Each radio keeps its identity, preferences, channels and
contacts under `~/.meshterm/radio/spidev1.0/` — `identity.key`, `prefs.json`,
`channels.json`, `contacts.json` — plus a running log, `node.log` (the previous run's is
kept as `node.log.1`). All of it survives a restart.

**A profile, if you want a name for it:**

```toml
[profiles.aio]
transport = "spi"
```

```bash
meshterm -p aio
```

The AIO's wiring is the default, so this profile needs nothing beyond `transport = "spi"`.
[Board wiring](#board-wiring) is for when yours differs.

---

## Moving over from the bridge

Already running the bridge? The first time you connect with `--spi` (or a profile, or the
picker row), MeshTerm carries the bridge's node over — **copying**, never moving, so the
bridge still works afterwards if you go back to it:

- the identity key — from the `meshcore-console` GUI's copy first
  (`~/.local/share/meshcore-uconsole/identity.key`), the way the bridge itself looks for
  it, else the bridge's own (`~/.local/share/meshterm-spi-bridge/identity.key`);
- the bridge's `contacts.json`;
- the name `uConsole`;
- the bridge's radio settings — frequency, bandwidth, spreading factor, coding rate, TX
  power — read out of the bridge service's `MESHCORE_*` environment.

This only happens once, while `~/.meshterm/radio/spidev1.0/` has no `identity.key` yet — so
your node keeps its public key, and with it its place in everyone else's contact list.

**Wiring is not carried over.** If your board needed `MESHCORE_EN_PINS` or another wiring
variable for the bridge, put the equivalent in the profile's `spi` table (see
[Board wiring](#board-wiring)) — the direct connection reads wiring from there, never from
the bridge's environment.

**Stop the bridge before switching over**, since only one program may hold the radio at a
time:

```bash
systemctl --user stop meshterm-spi-bridge
systemctl --user disable meshterm-spi-bridge   # keep it stopped across reboots
```

If MeshTerm sees the bridge service running, it refuses to start the node and tells you the
same two commands.

---

## Board wiring

The defaults match the hackergadgets uConsole AIO **v1**, which needs no `spi` table at
all. State only what differs, under `[profiles.<name>.spi]`:

```toml
[profiles.aio2]
transport = "spi"

[profiles.aio2.spi]
en_pins = [27]   # the AIO v2 needs this
```

| Setting | Default | What it is |
| --- | --- | --- |
| `bus_id`, `cs_id` | `1`, `0` | Which SPI bus and chip-select the radio is on (`/dev/spidev<bus_id>.<cs_id>`). |
| `cs_pin` | `-1` | A GPIO driven as chip select by hand, instead of the bus's own. |
| `gpio_chip` | `0` | Which `/dev/gpiochip<n>` the pins below are on. |
| `use_gpiod_backend` | `false` | Drive the pins through `gpiod` instead of `python-periphery`. |
| `reset_pin`, `busy_pin`, `irq_pin` | `25`, `24`, `26` | The chip's reset, busy and interrupt (DIO1) lines. |
| `txen_pin`, `rxen_pin` | `-1`, `-1` | An external RF switch's transmit/receive-enable lines, if the board has one. |
| `en_pins` | `[]` | Power-enable lines to raise before the chip is touched. The **AIO v2 needs `[27]`.** |
| `use_dio2_rf`, `use_dio3_tcxo` | `true`, `true` | Whether DIO2 drives the RF switch and DIO3 a TCXO (both on for the AIO). |
| `is_waveshare` | `false` | The Waveshare HAT's wiring quirks, which the radio library knows about. |
| `python` | `""` (empty) | An interpreter to run the node under, when the one MeshTerm would find on its own isn't the one you want. Empty lets MeshTerm look. |

An unknown key, or one of the wrong type, stops MeshTerm at startup with the profile
named — a misspelt pin is never silently ignored.

Radio settings — frequency, bandwidth, spreading factor, coding rate, TX power — are
**not** in this table. They're the node's own, on Device config, as noted in
[What you need](#what-you-need).

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| The radio library isn't found | Follow [Step 2](#step-2--install-the-radio-library) for however you installed MeshTerm, or set `python` in the profile's `spi` table to an interpreter that has `openhop-core[hardware]`. |
| No `/dev/spidev1.0` | Add `dtoverlay=spi1-1cs` to `/boot/firmware/config.txt` and reboot. |
| Permission denied opening the SPI or GPIO device | `sudo usermod -aG spi,gpio $USER`, then log out and back in. |
| "the meshterm-spi-bridge service has the radio" | Stop it: `systemctl --user stop meshterm-spi-bridge` (and `disable` it to keep it stopped). |
| "meshcore-console has the radio open" | Close the GUI and connect again. |
| Some other program has the radio | `sudo lsof /dev/gpiochip0` shows which; only one program may hold the radio at a time. |
| The node starts but never answers | Check its log: `~/.meshterm/radio/spidev1.0/node.log`. |
| An AIO v2 radio never comes up | It needs `en_pins = [27]` in the profile's `spi` table — see [Board wiring](#board-wiring). |

---

## Always on: the bridge

Most people should use the direct connection above. Use the bridge instead if you want the
node reachable on the mesh **while MeshTerm is closed** — it runs as a `systemd --user`
service, on its own, independent of whether MeshTerm is even installed. The trade-off runs
the other way from the direct connection: once the bridge is running, it holds the radio's
pins for good, so no other LoRa program — including a direct `meshterm --spi` — can use
them until you stop it.

The bridge puts the standard MeshCore companion protocol in front of the same SPI-attached
chip, on a local TCP port. MeshTerm then connects to that port like it would any network
companion.

- [Bridge step 1 — Install the radio runtime](#bridge-step-1--install-the-radio-runtime)
- [Bridge step 2 — Get the bridge](#bridge-step-2--get-the-bridge)
- [Bridge step 3 — Run the preflight](#bridge-step-3--run-the-preflight)
- [Bridge step 4 — Run it once in the foreground](#bridge-step-4--run-it-once-in-the-foreground)
- [Bridge step 5 — Install it as a service](#bridge-step-5--install-it-as-a-service)
- [Bridge step 6 — Connect MeshTerm to it](#bridge-step-6--connect-meshterm-to-it)
- [Updating and uninstalling the bridge](#updating-and-uninstalling-the-bridge)
- [Bridge hardware knobs](#bridge-hardware-knobs)
- [Bridge troubleshooting](#bridge-troubleshooting)

The bridge's own wiring and radio defaults are the same AIO v1 numbers as the direct
connection's — see the table in [Board wiring](#board-wiring) — just set through
`MESHCORE_*` environment variables instead of a profile's `spi` table.

The radio library that actually drives the chip was renamed in 2026: `pymc_core` became
`openhop_core`. PyPI's `pymc-core` stopped at 1.0.12; `openhop-core` carries on from 1.1.x.
The bridge supports both and prefers the newer one when it finds both installed.

### Bridge step 1 — Install the radio runtime

Which route you take depends on your Debian release:

- **trixie**, with the `meshcore-uconsole` package (by cwill747) installed: you already
  have a working runtime (Python 3.13-based). Skip to
  [step 2](#bridge-step-2--get-the-bridge).
- **bookworm**: the trixie `.deb` of `meshcore-uconsole` 1.12.0 is built on Python 3.13
  and needs glibc 2.38, so it will not install on bookworm, and the APT repository offers
  nothing for bookworm. Upstream's GitHub Releases also carry a separate bookworm `.deb`
  built on Python 3.11; it has not been tested with the bridge. The documented route is to
  give the bridge a runtime of its own:

  ```bash
  python3 -m venv ~/.local/share/meshterm-spi-bridge/venv
  ~/.local/share/meshterm-spi-bridge/venv/bin/pip install "openhop-core[hardware]"
  ```

This is also the route to take if you'd rather not install the `meshcore-uconsole` package
at all.

You'll confirm which runtime the bridge actually found in
[step 3](#bridge-step-3--run-the-preflight) — its first check line names the interpreter
and the runtime.

### Bridge step 2 — Get the bridge

`scripts/uconsole/meshterm-spi-bridge` is a single, self-contained Python file — no install step of
its own. Either clone the MeshTerm repo:

```bash
git clone https://github.com/jpmartineau/MeshTerm
cd MeshTerm/scripts/uconsole
```

or download just the one file:

```bash
curl -L -o meshterm-spi-bridge \
  https://raw.githubusercontent.com/jpmartineau/MeshTerm/main/scripts/uconsole/meshterm-spi-bridge
```

Make it executable:

```bash
chmod +x meshterm-spi-bridge
```

### Bridge step 3 — Run the preflight

```bash
./meshterm-spi-bridge --check
```

This prints a `Prerequisite check:` block with one line per check. What each line means and
what to do if it fails:

| Check | Meaning if it fails | Fix |
| --- | --- | --- |
| `node runtime` | Neither `openhop_core` nor `pymc_core` is importable by any interpreter the bridge tried. | Build the venv from [step 1](#bridge-step-1--install-the-radio-runtime), or set `MESHTERM_PYMC_PYTHON` to a python that has one. |
| `SPI device` | No `/dev/spidev*` node exists, or it exists but you can't read/write it. | No node at all: add `dtoverlay=spi1-1cs` to `/boot/firmware/config.txt` and reboot. Node present but no permission: `sudo usermod -aG spi $USER`, then log out and back in. |
| `GPIO chip` | `/dev/gpiochip0` is missing or not read/write for you. | `sudo usermod -aG gpio $USER`, then log out and back in. |
| `bridge service` / `radio in use` / `port 5000 busy` / `radio is free` | Tells you whether something already holds the radio: the bridge's own boot service, a running `meshcore-console`, or something else already listening on the TCP port. | Only one program may use the radio at a time — see [step 6](#bridge-step-6--connect-meshterm-to-it). If `meshcore-console` is running, close it before running the bridge. |

If the first three checks all pass, you're ready to run it.

### Bridge step 4 — Run it once in the foreground

```bash
./meshterm-spi-bridge --run
```

This runs the preflight again and prints it. Before it starts the bridge, it asks whether
to add a `bridge` profile to your MeshTerm config
(`` Add a 'bridge' profile to /home/you/.meshterm/config.toml so you can just run `meshterm`? [y/N] ``).
Either answer is fine; step 5 asks again if you say no. Then it starts the bridge in this
terminal. A healthy startup logs something like:

```
node runtime openhop_core 1.1.3 (/home/you/.local/share/meshterm-spi-bridge/venv/bin/python)
radio config bus_id=1 reset_pin=25 busy_pin=24 irq_pin=26 frequency=910525000 tx_power=22
radio.begin() attempt 1/4
radio ready
node identity ab12cd34ef567890… (hash 0xab, name 'uConsole')
READY — connect with:  meshterm --tcp 127.0.0.1:5000
```

The `radio config` line lists every setting as `key=value`, so the one above is
shortened; yours is longer.

You can test the bridge this way: leave it running, and in another terminal on the same
machine run:

```bash
meshterm --tcp 127.0.0.1:5000
```

Back in the bridge's terminal, press **Ctrl+C** to stop it.

### Bridge step 5 — Install it as a service

Run the bridge with no flags to get the interactive menu:

```bash
./meshterm-spi-bridge
```

Choose **2) Install as a service**. This:

- copies the script to `~/.local/bin/meshterm-spi-bridge`,
- writes a `systemd --user` unit at
  `~/.config/systemd/user/meshterm-spi-bridge.service` that restarts on failure and starts
  with your session,
- enables and starts it now,
- tries to enable lingering for your user, so the service comes up at boot even before you
  log in. If it can't, it prints the command to run yourself:

```bash
sudo loginctl enable-linger $USER
```

It also offers to add a `[profiles.bridge]` block to your MeshTerm `config.toml` — say yes,
it's the profile you'll use next.

Check on it any time:

```bash
systemctl --user status meshterm-spi-bridge.service
journalctl --user -u meshterm-spi-bridge.service -f
```

Menu option **4) Show status** prints the same information from inside the script.

### Bridge step 6 — Connect MeshTerm to it

Install MeshTerm on the uConsole the same way as [Step 3](#step-3--install-meshterm)
above — the bridge doesn't need the `spi` extra or `openhop-core[hardware]` in MeshTerm's
own Python, since the bridge is the one running the node.

If you accepted the profile in step 4 or 5, `~/.meshterm/config.toml` now has this block
(the bridge always writes to that path, even if you set `MESHTERM_HOME`):

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

or, without a profile:

```bash
meshterm --tcp 127.0.0.1:5000
```

You're connected when **Device info** or the dashboard shows a node name (`uConsole` by
default) instead of a connection error. MeshTerm reports the device model as
`pyMC-spi-bridge` — that's how you tell a bridged node from a real companion or a direct
SPI connection.

**Identity.** If the `meshcore-console` GUI's package is present, the bridge loads *that*
package's identity key, so MeshTerm and the GUI share the same public key. Contacts and
radio settings are the bridge's own: it keeps contacts in
`~/.local/share/meshterm-spi-bridge/contacts.json`, its channels in
`~/.local/share/meshterm-spi-bridge/channels.json` (both survive a restart), and builds its
radio settings from the `MESHCORE_*` variables and its defaults, never from the GUI's saved
settings. Set the `MESHCORE_*` variables to match what the GUI uses. On a library-only
install, the bridge mints and keeps its own identity key under
`~/.local/share/meshterm-spi-bridge/identity.key`.

**Only one program may use the radio at a time.** Never run the bridge and the
`meshcore-console` GUI at the same time — whichever started second will fail to open the
radio. The same goes for a direct `meshterm --spi` connection while the bridge is running.

### Updating and uninstalling the bridge

To update, pull or re-download `meshterm-spi-bridge` and reinstall the service (menu option
2 again — it overwrites the copy in `~/.local/bin/`). The running service keeps the old
code until you restart it:

```bash
systemctl --user restart meshterm-spi-bridge.service
```

Option 2 also rewrites the unit file, so any `Environment=` lines you added to it by hand
are lost. Put them in a drop-in instead, as [Bridge hardware knobs](#bridge-hardware-knobs)
shows.

To remove the bridge, run the menu and choose **3) Uninstall the service**. This stops and
removes the `systemd --user` unit and the installed script copy. It does **not** touch
`meshcore-console`, the radio library, your node identity, or your saved contacts and
channels — those stay put. If you'd added `[profiles.bridge]` to your MeshTerm config, it
tells you to remove that block by hand if you no longer want it.

### Bridge hardware knobs

The pin defaults match the hackergadgets uConsole AIO v1, which needs none of these. The
**AIO v2 needs `MESHCORE_EN_PINS=27`**, whether or not the `meshcore-console` package is
installed. The radio settings default to the US/Canada preset. For a foreground run, export
what differs before launching the bridge. For the service, add a drop-in, which survives a
reinstall:

```bash
systemctl --user edit meshterm-spi-bridge.service
```

and put the variables under a `[Service]` heading, one `Environment=` line each:

```ini
[Service]
Environment=MESHCORE_EN_PINS=27
```

Then `systemctl --user restart meshterm-spi-bridge.service`.

| Variable | What it sets |
| --- | --- |
| `MESHCORE_BUS_ID`, `MESHCORE_CS_ID`, `MESHCORE_CS_PIN` | which SPI bus, device and chip-select pin the radio is on |
| `MESHCORE_RESET_PIN`, `MESHCORE_BUSY_PIN`, `MESHCORE_IRQ_PIN` | the three control lines |
| `MESHCORE_FREQUENCY`, `MESHCORE_TX_POWER` | frequency in Hz, power in dBm |
| `MESHCORE_SPREADING_FACTOR`, `MESHCORE_BANDWIDTH`, `MESHCORE_CODING_RATE` | the modem preset — all three must match the mesh you are joining. Whole numbers only: bandwidth in Hz (`62500`), coding rate as the denominator (`5` for 4/5). A decimal or `4/5` stops the bridge at startup. |
| `MESHCORE_TXEN_PIN`, `MESHCORE_RXEN_PIN`, `MESHCORE_EN_PINS` | the RF-switch and power-enable lines a board may need (`-1` for none; `EN_PINS` is a comma list — the AIO v2 wants `27`) |
| `MESHCORE_USE_DIO2_RF`, `MESHCORE_USE_DIO3_TCXO`, `MESHCORE_IS_WAVESHARE` | whether DIO2 drives the RF switch and DIO3 the TCXO (both on for the AIO), and a flag for a different vendor's wiring the runtime knows about |
| `MESHCORE_GPIO_CHIP`, `MESHCORE_USE_GPIOD_BACKEND`, `MESHCORE_PREAMBLE_LENGTH` | which gpiochip, whether to drive it through `gpiod`, and the LoRa preamble (leave it unset: the bridge follows MeshCore, 32 symbols up to SF8 and 16 above, and a shorter one leaves the radio deaf to most of the mesh) |
| `MESHTERM_PYMC_PYTHON` | force a specific interpreter instead of letting the bridge discover one |

The bridge passes a knob only when the runtime's radio constructor accepts it, so the same
environment works under `pymc_core` 1.0.x, which lacks the newer ones. Where the
`meshcore-console` package is installed, the bridge builds the radio through that
package's environment reader, which reads the same names plus a few more of its own (see
that package's documentation) and ignores the GUI's saved presets. On a library-only
install the table above is the whole of it. Either way, an AIO v2 without
`MESHCORE_EN_PINS=27` will not power its radio.

### Bridge troubleshooting

| Symptom | Check |
| --- | --- |
| `node runtime` fails in the preflight | Did you build the venv in [step 1](#bridge-step-1--install-the-radio-runtime)? Is `MESHTERM_PYMC_PYTHON` pointing at the right interpreter, if set? |
| `SPI device` shows "no /dev/spidev* found" | Is `dtoverlay=spi1-1cs` in `/boot/firmware/config.txt`? Did you reboot after adding it? |
| `SPI device` or `GPIO chip` shows a permission error | Are you in the `spi` and `gpio` groups (`groups`)? Did you log out and back in after `usermod`? |
| Preflight warns `radio in use         meshcore-console is running:` | Close the GUI before running or installing the bridge. |
| Preflight warns `port 5000 busy         something is already on 127.0.0.1:5000` | Something other than the bridge service is listening there (an active service is reported as `bridge service running` instead). Usually it is a foreground `--run` in another terminal; stop that one. |
| Bridge exits with `bridge failed: GPIO pins still busy after retries` | The bridge retries `radio.begin()` four times only when the GPIO lines are busy. They may still be held by a session that just closed — wait a few seconds and try again. |
| MeshTerm connects but direct messages to an old contact say "not found" | Contacts are restored from the bridge's snapshot file at startup. If the contact is not in it, wait until the contact is heard again. |
| Reboot, factory reset, or setting a device PIN silently does nothing | Expected — the radio library has no handler for these three, whatever runtime you're on. |
| Traces or the live feed don't show up | These rely on compatibility shims the bridge installs (or, under `openhop_core`, on the library's own equivalents). Check the bridge's log for lines starting `compat:` to confirm they're wired. |

---

## What this has been tested on

**The direct connection** has been tested against the real radio library
(`openhop-core`) with a simulated radio; it has **not yet been verified on uConsole
hardware**.

**The bridge** has been verified on a bookworm uConsole with `openhop-core` 1.1.3: the
radio comes up, contacts are restored across a restart, and `info` and `contacts` answer
correctly over the TCP connection. The older `pymc-core` 1.0.x path, with all three of the
bridge's compatibility shims active, has been exercised in full, including traces and the
live feed. MeshTerm's test suite does not cover the bridge.

What has not been confirmed:

- **The direct connection on real hardware** — everything above has only run against a
  simulated radio under the real library.
- **The bridge under `openhop_core` beyond `info` and `contacts`.** Startup, identity,
  contact restore and those two reads were exercised; a trace, a message and the live feed
  under the new runtime were not, though the library's own trace push is what the bridge
  now relies on for the first.
- **Everything on-air** — the per-hop trace semantics the bridge reassembles and the
  acknowledgement bytes newer firmware appends after the CRC. The code is internally
  consistent with the library's API; the wire behaviour was seen on one mesh, against the
  radios in range of one bench.
