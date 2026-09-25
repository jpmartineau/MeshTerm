# Configuring MeshTerm

Preferences, device profiles, and where everything is kept. You need none of it to run
MeshTerm: it discovers a companion, lets you pick one, and remembers the choice.

Two files, two jobs — and you need neither to run.

**Preferences** are how MeshTerm behaves: the pause it leaves between transmissions, how
hard it retries a message, how far back it keeps history, how a map frames itself. Every one has a built-in default, so change them only where you disagree — from
the **Preferences** page in the menu (grouped, staged, saved by one action at the bottom),
or from a shell:

```console
$ meshterm preferences show                 # every preference, its value, its default
$ meshterm preferences set history_days 90
$ meshterm preferences reset --yes          # back to the built-in defaults
```

They are kept in `~/.meshterm/preferences.toml`, which lists only what you have changed;
delete a line and the default takes over again.

**Config** is where things live and which device to talk to. MeshTerm runs with none of it
— it discovers attached serial devices and nearby Bluetooth companions, lets you pick one,
and remembers the last good default. A network (TCP) companion isn't discoverable, so reach
it with `--tcp host:port`, a TCP profile, or the picker's "add a network device" prompt.
A LoRa radio on this machine's own SPI bus is listed on the picker when its device node
exists, or reached with `--spi`; see [the uConsole manual](uconsole.md).
Write a config file only to give your hardware stable aliases.

Copy [`config.example.toml`](../config.example.toml) to `~/.meshterm/config.toml`:

```toml
# default_profile = "s3"   # uncomment and name your own profile to make it the default
connect_on_start = true   # false opens the radio link lazily instead of at launch

[profiles.s3]
port = "COM5"
baudrate = 115200
description = "XIAO ESP32-S3 + Wio SX1262 serial companion"

# A Bluetooth LE companion: give it an `address` instead of a `port`.
[profiles.handheld]
address = "AA:BB:CC:DD:EE:FF"
# ble_pin = "123456"   # only if your device requires a pairing PIN
description = "Pocket handheld over Bluetooth"

# A network (TCP) companion: give it a `host` (and optional `tcp_port`, default 5000).
[profiles.wifi]
host = "192.168.1.50"
description = "Basestation over WiFi"

# A radio on this machine's own SPI bus (the uConsole AIO): MeshTerm runs its node.
# The pins default to the AIO v1's; a `spi` table states only what differs.
[profiles.aio]
transport = "spi"
# [profiles.aio.spi]
# en_pins = [27]   # the AIO v2 powers its radio from pin 27
```

Both live in `~/.meshterm`, along with everything else MeshTerm remembers: the SQLite
history, the outbox, the contact and channel caches, stored admin passwords, and the log.
**`$MESHTERM_HOME` moves the whole directory**, which is the way to run a second radio —
or a `--mock` session — without touching the one you use every day. `--db` moves the
database alone; the rest stays where it was. A `--mock` run that names neither writes to
`meshterm-mock.db` beside the real history, so the simulator's invented nodes never enter
it. [`docs/cli.md`](cli.md#where-meshterm-keeps-its-state)
lists every file.

Timestamps are stored as UTC and rendered in your local time. Heard packets older than the
`history_days` preference are deleted when monitoring starts: once in each menu session,
and at the start of each `meshterm monitor` run. Nothing else is pruned.

## Adding a radio on the SPI bus

Some boards put the LoRa chip straight on the computer's SPI bus, with no firmware in front
of it: the uConsole's AIO board, and LoRa HATs for the Raspberry Pi. MeshTerm drives these
itself — it runs the node while it is connected and lets go of the radio when it quits.
It needs Linux and the radio library; [the uConsole manual](uconsole.md#step-2--install-the-radio-library)
shows how to install that.

**An AIO v1 needs no configuration.** Its wiring is MeshTerm's default, so whenever
`/dev/spidev1.0` exists the device screen lists it as **SPI radio**, and `meshterm --spi`
reaches it. Any other board is added with a profile that states how it is wired.

**Why a profile, and not a prompt on the device screen.** A USB companion describes itself:
plug it in and there is nothing to ask. An SPI radio says nothing about how it is wired, and
the wiring is a dozen facts — the bus, the chip select, the reset, busy, and interrupt pins,
the antenna switch, the TCXO — any one of which, wrong, leaves a radio that comes up deaf
rather than failing. Those are facts to look up once and write down, not to type into a
dialog. And the radio doesn't come and go the way a cable does: it is soldered, stacked on
a header, or built into the case, so its description belongs somewhere as lasting as the
hardware is. A file you write once, and can read back when something is off, is that place.

### 1. Find your board's wiring

The easiest place to find it is the board's `meshtasticd` configuration, which most vendors
publish (usually a `config.yaml` or a `config.d/` fragment with a `Lora:` section). Its keys
map one to one:

| `meshtasticd` (`Lora:`) | MeshTerm (`[profiles.<name>.spi]`) |
| --- | --- |
| `spidev: spidev0.0` | `bus_id = 0` and `cs_id = 0` — the two numbers of `spidevB.C` |
| `CS: 21` | `cs_pin = 21` (only when the board drives chip select from a GPIO) |
| `Reset`, `Busy`, `IRQ` | `reset_pin`, `busy_pin`, `irq_pin` |
| `TXen`, `RXen` | `txen_pin`, `rxen_pin` |
| `DIO2_AS_RF_SWITCH: true` | `use_dio2_rf = true` |
| `DIO3_TCXO_VOLTAGE: true` | `use_dio3_tcxo = true` (1.8 V) |
| `gpiochip: 4` | `gpio_chip = 4` |

`Module` must be `sx1262`; that is the chip MeshTerm's node drives. `gpio_chip` is `0` on
a Compute Module 4 but differs on a Raspberry Pi 5 and a CM5 — `gpiodetect`, or
`ls /dev/gpiochip*`, shows which chip carries the 40-pin header.

**State every pin and both switches.** Where a `meshtasticd` file leaves a key out, it means
"none" or "off". Where the `spi` table leaves a key out, it means *the AIO's value* — IRQ on
26, DIO2 and DIO3 on — which is wrong for almost any other board, and a radio wired wrong
comes up deaf rather than failing.

### 2. Write the profile

In `~/.meshterm/config.toml`, give the radio a name and its wiring. The values below are an
illustration, not any particular board's — take yours from step 1:

```toml
[profiles.hat]
description = "SX1262 HAT on the Pi header"

[profiles.hat.spi]
bus_id = 0              # /dev/spidev0.0
cs_id = 0
reset_pin = 18
busy_pin = 20
irq_pin = 16
txen_pin = 6            # this HAT switches its antenna from a GPIO…
rxen_pin = -1
use_dio2_rf = false     # …so DIO2 doesn't
use_dio3_tcxo = false   # no TCXO on this board
```

A `spi` table is enough to make it an SPI profile; `transport = "spi"` says so outright and
is what an AIO profile with no table uses. Every key, with the AIO defaults, is in
[the uConsole manual's wiring table](uconsole.md#board-wiring). A key MeshTerm doesn't know,
or a value of the wrong type, stops it at startup with the profile named — a misspelt pin
is never quietly ignored.

### 3. Check it

```bash
meshterm -p hat info
```

The first run starts the node, which mints the radio's identity. If something is wrong, the
error says what to fix — a missing `/dev/spidev*` (enable SPI and reboot), a permission
problem (join the `spi` and `gpio` groups), a missing radio library, or another program
already holding the radio. A node that starts but never answers leaves its log in
`~/.meshterm/radio/spidev0.0/node.log`. Once it answers, the device screen lists the radio
under the profile's name.

### What the profile doesn't hold

- **Radio settings.** Frequency, bandwidth, spreading factor, coding rate, and TX power are
  the node's own settings, saved by the node and changed on **Device config** like any
  companion's. A new node starts on MeshCore's **US/Canada preset** (910.525 MHz, 62.5 kHz,
  SF7, CR 4/5, 22 dBm): anywhere else, change it on Device config before you send anything.
- **The preamble.** The node follows MeshCore's own rule — 32 symbols up to SF8, 16 above —
  and a receiver that expects anything else misses most of the mesh. `preamble_length` was
  once a wiring key and is refused now; delete the line if an old profile has it.
- **Identity.** The node's key, contacts, and channels live in
  `~/.meshterm/radio/<spidev>/`, filed by the device node rather than the profile, so
  renaming a profile, or reaching the same radio from the device screen, keeps the same node.
