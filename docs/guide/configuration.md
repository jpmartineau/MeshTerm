# Configuring MeshTerm

This page covers preferences, device profiles, and the place where MeshTerm keeps each
file. You do not need any of it to run MeshTerm. MeshTerm discovers a companion, lets you
select one, and remembers your choice.

MeshTerm has two files for two jobs. You need neither file to run MeshTerm.

**Preferences** are how MeshTerm behaves. Examples are the pause between transmissions, how
many times MeshTerm tries to send a message again, how far back it keeps history, and how
the map frames itself. Each preference has a built-in default. Change a preference only
where you do not agree with the default. You can change preferences on the **Preferences**
page in the menu. The page stages your changes, and you save them with one action at the
bottom. You can also change them from a shell:

```console
$ meshterm preferences show                 # every preference, its value, its default
$ meshterm preferences set history_days 90
$ meshterm preferences reset --yes          # back to the built-in defaults
```

MeshTerm keeps the preferences in `~/.meshterm/preferences.toml`. This file lists only the
preferences that you changed. If you delete a line, the default applies again.

A preference is not a setting. A **setting** is a value of the radio. The device keeps it,
and you edit it on Device config.

**Config** is the setup of the machine: where files are, and which device to talk to.
MeshTerm runs without it. It discovers the serial devices that are attached and the
Bluetooth companions that are near. It lets you select one, and it remembers the last good
default. A network (TCP) companion cannot be discovered. To reach one, use `--tcp
host:port`, a TCP profile, or the "add a network device" prompt of the picker. The picker
lists a LoRa radio on the SPI bus of this machine when its device file exists. You can also
reach it with `--spi`. For more information, refer to [the uConsole manual](../devices/uconsole.md).
Write a config file only to give your hardware stable aliases. You edit this file with a
text editor.

Copy [`config.example.toml`](../../config.example.toml) to `~/.meshterm/config.toml`:

```toml
# default_profile = "s3"   # uncomment and name your own profile to make it the default
connect_on_start = true   # false opens the radio link lazily instead of at launch
# db_path = "C:/Users/you/meshterm/meshterm.db"   # the history lives elsewhere

[profiles.s3]
port = "COM5"
baudrate = 115200
description = "XIAO ESP32-S3 + Wio SX1262 serial companion"

# A Bluetooth LE companion: give it an `address` instead of a `port`.
[profiles.handheld]
address = "AA:BB:CC:DD:EE:FF"
# ble_pin = "123456"   # only if your device requires a pairing PIN (not used on macOS)
description = "Pocket handheld over Bluetooth"

# A network (TCP) companion: give it a `host` (and optional `tcp_port`, default 5000).
[profiles.wifi]
host = "192.168.1.50"
description = "Basestation over Wi-Fi"

# A radio on this machine's own SPI bus (the uConsole AIO): MeshTerm runs its node.
# The pins default to the AIO v1's; a `spi` table states only what differs.
[profiles.aio]
transport = "spi"
# [profiles.aio.spi]
# en_pins = [27]   # the AIO v2 powers its radio from pin 27
```

Both files are in `~/.meshterm`. MeshTerm also keeps everything else that it remembers
there: the SQLite history, the outbox, the caches of contacts and channels, the stored
admin passwords, and the log.

**`$MESHTERM_HOME` moves the whole directory.** Use it to run a second radio, or a `--mock`
session, without a change to the files that you use each day. `--db` moves only the
database. The other files stay where they were. A `--mock` run that has neither option
writes to `meshterm-mock.db` beside the real history. Thus the invented nodes of the
simulator never enter the real history.
[`docs/cli.md`](../cli/README.md#where-meshterm-keeps-its-state) lists each file.

MeshTerm stores timestamps as UTC and shows them in your local time. MeshTerm deletes
heard packets that are older than the `history_days` preference when monitoring starts.
This happens one time in each menu session, and at the start of each `meshterm monitor`
run. MeshTerm deletes no other data in this way.

## Adding a radio on the SPI bus

Some boards put the LoRa chip directly on the SPI bus of the computer, with no firmware in
front of it. Examples are the AIO board of the uConsole, and LoRa HATs for the Raspberry
Pi. MeshTerm drives these radios itself. It runs the node while it is connected, and it
releases the radio when it quits. MeshTerm needs Linux and the radio library.
[The uConsole manual](../devices/uconsole.md#step-2-install-the-radio-library) shows how
to install the library.

**Two boards need no configuration.** The wiring of an AIO v1 is the MeshTerm default.
Thus, when `/dev/spidev1.0` exists, the device screen lists it. The name is **uConsole AIO**
on a uConsole, and **SPI radio** on any other machine. MeshTerm also includes the Cap
LoRa-1262 of M5Stack on a Cardputer Zero. The device screen lists it as **Cap LoRa-1262**
on a Cardputer Zero (refer to [the Cardputer Zero page](../devices/cardputer-zero.md#the-cap-lora-1262)).
For either board, `meshterm --spi` reaches the radio. To add any other board, write a
profile that states how the board is wired. A profile on the same `/dev/spidev*` replaces
the wiring that MeshTerm includes.

**Why a profile, and not a prompt on the device screen.** A USB companion describes itself.
When you connect it, MeshTerm has nothing to ask. An SPI radio does not tell how it is
wired. The wiring is a dozen facts: the bus, the chip select, the reset, busy, and
interrupt pins, the antenna switch, and the TCXO. If any one of these is wrong, the radio
starts but cannot hear. It does not fail with an error. You look up these facts one time
and write them down. You do not type them into a dialog. Also, an SPI radio does not come
and go as a cable does. It is soldered, stacked on a header, or built into the case. Thus
its description must last as long as the hardware lasts. A file that you write one time,
and read again when something is wrong, is the correct place.

### 1. Find your board's wiring

The easiest place to find the wiring is the `meshtasticd` configuration of the board. Most
vendors publish it. It is usually a `config.yaml` or a fragment in `config.d/` that has a
`Lora:` section. The keys map one to one:

| `meshtasticd` (`Lora:`) | MeshTerm (`[profiles.<name>.spi]`) |
| --- | --- |
| `spidev: spidev0.0` | `bus_id = 0` and `cs_id = 0`, the two numbers of `spidevB.C` |
| `CS: 21` | `cs_pin = 21` (only when the board drives chip select from a GPIO) |
| `Reset`, `Busy`, `IRQ` | `reset_pin`, `busy_pin`, `irq_pin` |
| `TXen`, `RXen` | `txen_pin`, `rxen_pin` |
| `DIO2_AS_RF_SWITCH: true` | `use_dio2_rf = true` |
| `DIO3_TCXO_VOLTAGE: true` | `use_dio3_tcxo = true` (1.8 V) |
| `gpiochip: 4` | usually nothing (refer to the text below), or `gpio_chip = 4` to set it |

`Module` must be `sx1262`, because the node of MeshTerm drives that chip. **Leave out
`gpio_chip`** unless you must override it. A `meshtasticd` file sets a number because
Meshtastic cannot look up the chip. But the number of the chip that has the 40-pin header
of a Raspberry Pi is not the same on a Pi 4/CM4 and a Pi 5/CM5. On the Pi 5/CM5 it also
changed between kernel releases. When you leave it out, it defaults to `-1`. Then MeshTerm
finds the chip of the header by its label (`pinctrl-rp1`, `pinctrl-bcm2711`, or
`pinctrl-bcm2835`). If a board has no chip with one of these labels, MeshTerm uses chip
`0`. `gpiodetect` lists each chip with its label. The
[uConsole guide](../devices/uconsole.md#which-gpio-chip) has the whole story.

**State each pin and both switches.** In a `meshtasticd` file, a key that is left out means
"none" or "off". In the `spi` table, a key that is left out means the value of the AIO.
For example, IRQ is on pin 26, and DIO2 and DIO3 are on. These values are wrong for almost
any other board. A radio with the wrong wiring starts but cannot hear. It does not fail
with an error.

### 2. Write the profile

In `~/.meshterm/config.toml`, give the radio a name and its wiring. The values below are an
example. They are not the values of a particular board. Take your values from step 1:

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

A `spi` table is enough to make the profile an SPI profile. `transport = "spi"` says so
directly. An AIO profile that has no table uses it. [The wiring table in the uConsole
manual](../devices/uconsole.md#board-wiring) has each key with its AIO default. If a key
is not known to MeshTerm, or a value has the wrong type, MeshTerm stops at startup and
names the profile. MeshTerm never ignores a misspelt pin without telling you.

### 3. Check it

```bash
meshterm -p hat info
```

The first run starts the node, which creates the identity of the radio. If something is
wrong, the error message tells you what to correct. These are the possible problems:

- A `/dev/spidev*` file is missing. Enable SPI and reboot.
- A permission problem. Join the `spi` and `gpio` groups.
- The radio library is missing.
- Another program already holds the radio.

If the node starts but never answers, it writes its log to
`~/.meshterm/radio/spidev0.0/node.log`. When the node answers, the device screen lists the
radio under the name of the profile.

### What the profile does not hold

- **Radio settings.** The frequency, bandwidth, spreading factor, coding rate, and TX power
  are settings of the node. The node saves them, and you change them on **Device config**,
  as for any companion. A new node starts on the **US/Canada preset** of MeshCore
  (910.525 MHz, 62.5 kHz, SF7, CR 4/5, 22 dBm). In any other region, change the preset on
  Device config before you send anything.
- **The preamble.** The node follows the rule of MeshCore: 32 symbols up to SF8, and 16
  symbols above SF8. A receiver that expects a different length misses most of the mesh.
  `preamble_length` was once a wiring key. MeshTerm now refuses it. If an old profile has
  this key, delete the line.
- **Identity.** The key, contacts, and channels of the node are in
  `~/.meshterm/radio/<spidev>/`. MeshTerm files them under the device file, not the
  profile. Thus, if you rename a profile, or you reach the same radio from the device
  screen, you keep the same node.
