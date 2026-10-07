# The MeshTerm command line

MeshTerm has two front ends on one codebase. The **menu** is a full-screen session for a
person to read. You get the menu when you run `meshterm` with no arguments. The **command
line** is the other front end, and this is its manual. It tells you what the command line
prints, what it returns, and each command that it accepts.

The command line prints in two ways. The way depends on who reads the output.

- The **plain face** is for a person at a prompt who typed a command to find something
  out. It is the output of a Unix utility: one record on each line, no colour, and no
  frames. It is easy to read. Names are bare, times read `5m` instead of
  `2026-09-08T04:26:00-04:00`, and a route is drawn with arrows.
- The **JSON face** (`--json`) is for a program, often on another machine and often
  later. It is the contract: it has typed values, it spells absent as `null`, its
  timestamps are in UTC, and its keys do not change. It works on **each** command.

Only `--json` promises that a program can read the output. The plain face does not make
this promise. If you split the output on whitespace to feed a script, stop. Use `--json`
instead, because the plain face can change between releases.

- [Running it](#running-it)
- [Where MeshTerm keeps its state](#where-meshterm-keeps-its-state)
- [About the examples](#about-the-examples)
- [The plain face](#the-plain-face)
- [The JSON face](#the-json-face)
- [Exit status](#exit-status)
- [When it goes wrong](#when-it-goes-wrong)
- [Command reference](#command-reference)
  - [Finding a device](#finding-a-device)
  - [This node](#this-node)
  - [The mesh around you](#the-mesh-around-you)
  - [Messaging](#messaging)
  - [Tracing](#tracing)
  - [Other nodes](#other-nodes)
  - [MeshTerm itself](#meshterm-itself)
- [Recipes](#recipes)
- [What has no command](#what-has-no-command)

---

## Running it

```
meshterm [GLOBAL OPTIONS] COMMAND [ARGS]
```

If you give no command, MeshTerm starts the interactive menu.

| Option | What it does |
| --- | --- |
| `--version` | Print `meshterm <version>` and exit `0`, before MeshTerm opens anything else. |
| `-p`, `--profile NAME` | Use a named device profile from `config.toml`. |
| `--port PORT` | The serial port to connect to (`COM5`, `/dev/ttyACM0`). It overrides the profile. |
| `--ble ADDRESS` | The Bluetooth address of a companion. It selects the Bluetooth transport. |
| `--ble-pin PIN` | The pairing PIN, if the Bluetooth companion asks for one. On Windows and Linux, MeshTerm pairs with the PIN itself. On macOS, the system asks for the code in its own dialog, and this option has no effect. Refer to [When a companion does not connect](../guide/connecting.md#bluetooth). |
| `--tcp HOST[:PORT]` | The network address of a TCP companion. It selects the TCP transport. The default port is 5000. |
| `--spi` | The LoRa radio on the SPI bus of this machine. It selects the SPI transport. MeshTerm runs the node for the length of the command. If one radio is attached, MeshTerm uses that radio, with a profile or without one. If no radio is attached, MeshTerm uses the one SPI profile, or else the wiring of the uConsole AIO. If two radios are attached, MeshTerm refuses (exit `3`) and lists them. Select one with `-p`. Refer to [Adding a radio on the SPI bus](../guide/configuration.md#adding-a-radio-on-the-spi-bus). Linux only. |
| `--mock` | Use the built-in simulator instead of real hardware. Nothing transmits, and the run records to `meshterm-mock.db` instead of your real history. |
| `--db PATH` | Use this SQLite database instead of `~/.meshterm/meshterm.db` (or `meshterm-mock.db`, with `--mock`). **It moves the database and nothing else.** Refer to [Where MeshTerm keeps its state](#where-meshterm-keeps-its-state). |
| `--json` | Print the answer as JSON instead of aligned text. |
| `--absolute` | For this run, print times as ISO-8601 instants instead of relative ages. |
| `-q`, `--quiet` | Do not print the log on the console. The log file still records it. |
| `--platform NAME` | Force the UI flavour (`regular`\|`picocalc-lyra`\|`cardputer-zero`) instead of detecting it. |

**You can type a global option anywhere.** You can type it before the command or after it.
`meshterm --json contacts` and `meshterm contacts --json` are the same run. The reason is
that `-o json` goes after the verb in each tool that has one, and `--json` is the first
option that most users try. MeshTerm does not move an option that follows a bare `--`.
The bare `--` is the standard way to say that the rest is data. MeshTerm also never moves
`--help`, so `meshterm contacts --help` stays the help of `contacts`.

A command that needs a radio opens the radio, does its work, and closes the radio. Some
commands only read stored history (`records`) or the state of MeshTerm itself
(`preferences`, `platform`, `specimen`, and the written pages). These commands need no
device. They work with nothing attached.

> **One transmission for each run.** A trace transmits exactly one time. Repeaters
> penalise nodes that send bursts of traffic, and they can blacklist these nodes. To get
> more samples, run the command again, with your own wait between the runs. `tx-optimize`
> is the one deliberate exception, and it paces itself.

---

## Where MeshTerm keeps its state

MeshTerm keeps all that it remembers in one directory. On Linux and macOS, the directory
is `~/.meshterm`. On Windows, it is `%USERPROFILE%\.meshterm`. **`$MESHTERM_HOME`
overrides it.** It is the only setting that moves the whole directory:

```bash
MESHTERM_HOME=/tmp/mesh-scratch meshterm contacts
```

MeshTerm reads the directory again on each run and does not cache it. Thus you can run two
radios from two directories in the same shell.

| File | What it holds |
| --- | --- |
| `meshterm.db` | The history: each packet that MeshTerm heard, each trace that it walked, and each message. **This is the one file that `--db` moves.** |
| `meshterm-mock.db` | The same history for the simulator. A `--mock` run records here when the run does not name a database. The file is absent until you do a `--mock` run. |
| `config.toml` | The setup of the machine: device profiles, and where the database is. You write this file. MeshTerm only reads it. |
| `preferences.toml` | Your overrides of the behaviour of MeshTerm. It lists only what you changed. |
| `courier.json` | The outbox: queued messages, both waiting and finished. |
| `admin.json` | The repeater-admin passwords that MeshTerm remembers. |
| `devices.json` | The default companion that MeshTerm remembers, and each device that spoke the protocol correctly. |
| `contacts.json`, `channels.json`, `settings.json` | The caches of each device. They hold what the radio last told MeshTerm, so a screen opens without a round trip to the radio. |
| `adverts.json` | The clock of the weekly flood advert: when the advert was switched on, and when the week of each device began. |
| `mutes.json`, `watchtower.json`, `remote.json` | The muted channels. The watched nodes and their alerts. The remote-admin cache of each node, and the CLI history. |
| `regions.json` | The regions that MeshTerm knows by name, and the repeaters that carry each region. Also the send scope of each channel, and the last answer of each repeater to "which regions do you carry?". MeshTerm uses this file to name the region of a scoped packet. |
| `radio/<spidev>/` | The node of an SPI radio. There is one folder for each device file (`radio/spidev1.0/`). It holds the identity key, the settings, the channels, the contacts, and the log (`node.log`). It is what the firmware keeps in flash. |
| `tilecache/` | The basemap tiles that MeshTerm downloaded. |
| `meshterm.log` | The log file. |
| `.lock` | The lock for a single instance. |

**`--db` alone does not isolate a run.** It moves the database. The database is most of
the data, but it does not hold the identity. The contact caches, the channel caches, the
outbox, the stored admin passwords, and the remembered device are all in the config
directory. They stay the same as the ones that you use each day. To run with a scratch
state (a test, a demo, or a second radio), set `MESHTERM_HOME`. If you want the database
in another place, set `--db` inside it. The same is true for the database that `--mock`
selects. It keeps the invented mesh of the simulator out of your history, but not out of
the caches.

---

## About the examples

**Each sample below is the output of a real run against the built-in simulator**
(`--mock`), in a temporary `MESHTERM_HOME`. The one exception is the `diagnostics`
sample, which says so where it appears. Nobody typed any of the output by hand. The
simulator answers as a companion does. It has four contacts (`Alice`, `Yagi-Repeater`,
`Local-Repeater`, `Observer-Bot`), it sends adverts, and it replies to traces. Thus the
shapes are real, but some numbers are invented.

The simulator does three things that a radio does not. You can see all three in the
samples:

- **Each run opens a new radio.** If one command writes a setting or fills a channel
  slot, the next command does not have it. Thus `config show` always reports the
  defaults, and `channels list` always answers `5`. On real hardware, the radio keeps
  the values.
- **`PKTS` in `contacts` stays `-`.** The tally of heard packets uses the node id that
  passive monitoring stores (four bytes here). The contact row uses the path hash width
  of the device (one byte). Thus the two do not match. Against real hardware with a
  matching width, they match.
- **A `trace` that the device routes invents its hop names.** If you give
  `trace --target NAME` with no `--path`, the simulator router returns placeholder hops.
  A forced path (`--path "a1,d4"`) resolves correctly. The samples use the forced form.
  Use the forced form also to build a golden file.

> **`--mock` records into its own history, not yours, but it still writes the caches.**
> The simulator is a fake radio. It is not a fake MeshTerm. MeshTerm stores what the
> simulator sends as adverts, in the same way as other data. The database that it writes
> is `meshterm-mock.db`. Thus the invented nodes stay out of your mesh walk, your
> dashboard, and your map. If you name a database with `--db`, that choice wins. The
> contact caches, the channel caches, the outbox, and the remembered devices are still
> the ones that you use each day. For a demo that must not touch anything, give the demo
> its own home: `MESHTERM_HOME=... meshterm --mock …`.

---

## The plain face

The plain face is the output of a Unix utility. It has one record on each line, nothing
folds, and stdout carries the answer and nothing else. The plain face has no rule that
exists only to make the output safe to split, because `--json` now does that job.

**No colour.** There is no hue, no bold, and no dim. Stdout has no escape sequence at
all, also when stdout is not a terminal. A colour that goes into a file is noise in the
file.

**No frames.** There are no borders, boxes, rules, titles, or legends. The command that
you typed is the title.

**Alignment is the delimiter, and names are bare.** A listing is an uppercase header line
and records that are aligned with spaces. This is the shape that `ps` and `df` print.
Numeric columns align to the right.

```console
$ meshterm contacts
NAME            TYPE      HEARD  PKTS  HASH  LOCATION            KEY
Observer-Bot    node         7m     -  c3    45.48800,-73.58100  c3d4e5f600000000000000000000000000000000000000000000000000000000
Local-Repeater  repeater     7m     -  b2    45.47680,-73.59900  b2c3d4e500000000000000000000000000000000000000000000000000000000
Yagi-Repeater   repeater     7m     -  a1    45.50190,-73.56740  a1b2c3d400000000000000000000000000000000000000000000000000000000
Alice           node         7m     -  d4    -                   d4e5f6a700000000000000000000000000000000000000000000000000000000
```

Names are not in quotation marks. Quotation marks would give a script a delimiter, and
they would fill each line with punctuation for the person who reads it. Names **are
escaped**. A node broadcasts its own name, and a stranger writes the body of a message.
Thus a name or a body must not hold a raw control character, and it must not end the
record that it is in.

**A time is an age**, because a person opens a listing to find out "what is recent?".
Examples are `now`, `5m`, `3h`, and `never`. An absolute instant stays where the instant
is the fact itself: the device clock, an appointment that you set with `--at`, and the
`TIME` column of a live capture.

**`--absolute` changes each age back to an instant.** The instant is local ISO-8601, to
the second:

```console
$ meshterm --absolute contacts
NAME            TYPE                           HEARD  PKTS  HASH  LOCATION            KEY
Observer-Bot    companion  2026-09-08T04:30:50-04:00     -  c3    45.48800,-73.58100  c3d4e5f600000000000000000000000000000000000000000000000000000000
Local-Repeater  repeater   2026-09-08T04:30:49-04:00     -  b2    45.47680,-73.59900  b2c3d4e500000000000000000000000000000000000000000000000000000000
Yagi-Repeater   repeater   2026-09-08T04:30:49-04:00     -  a1    45.50190,-73.56740  a1b2c3d400000000000000000000000000000000000000000000000000000000
Alice           companion  2026-09-08T04:30:48-04:00     -  d4    -                   d4e5f6a700000000000000000000000000000000000000000000000000000000
```

The flag **overrides a preference**. It is not a second switch that works beside the
preference. The preference `cli_time_format` is `relative` or `absolute`. The command
`meshterm preferences set cli_time_format absolute` makes `absolute` the default for each
run. `--absolute` sets it for one run and never saves it. A value of the behaviour of
MeshTerm is a preference, by the rule of this project. A flag that bypassed the registry
would be a behaviour that nobody can find.

**A route is drawn and a path is typed.** They are different things, and they look
different. These are two lines from the facts block of a trace:

```
path        a1,d4
route       MockCompanion (00) → Yagi-Repeater (a1) → Alice (d4) → MockCompanion (00)
```

A **path** is a spec. It is the list of hops that you write and force. It is the one line
on each face that you can send back as input, thus it stays as comma-separated hex, and
nothing else goes into it. Paste it into `--path` as it is. A **route** is the sequence
of nodes that a walk really took. You read a route and you do not type it, thus it is
drawn to be read. A hop is `Name (hash)`, or the one half that MeshTerm knows. It is never
an empty `()`.

**Our node is a hop like each other hop.** It has a name and a hash. It is never the
star of the menu. The star means "you already know who this is". This is true for the
person who watches, but it is not true for a person who opens the file later. The hash is
there because the CLI has no colour. On the screen, the hues that come from the keys show
which hop is which. In plain text, the hash carries that identity. The hash also joins a
route line to the table of hops under it.

**Key/value blocks** have a key, a gutter, and the rest of the line as the value. This is
the shape that `sysctl -a` prints. There is no header. The value is all the text that
follows the key.

```console
$ meshterm info
name              MockCompanion
public_key        0000000000000000000000000000000000000000000000000000000000000000
role              companion
battery_v         4.10
uptime_s          93784  (1d 2h)
noise_floor_dbm   -110
```

A reading can have a raw number as its fact, and a duration as its meaning. Then the
reading has an explanation in parentheses. `93784` is what the key promises and what a
caller wants. `(1d 2h)` is what a person wants to know. You do not have to parse one out
of the other.

A `get` prints the bare value alone, ready for `$(...)`, because the caller named the key:

```console
$ meshterm config get tx_power
20
```

**`-` is the one token for absent.** It means that the value is unknown, or that it does
not apply. You learn one token, and there is no em dash, no `n/a`, and no empty cell.
**`never` is different.** It is a value. It means that MeshTerm has not heard the node
yet. A node type stays the word `repeater`, because a monochrome glyph would need a
legend, and the CLI has no legends.

**A live stream fixes its lanes.** `monitor` and `chat listen` cannot measure a column
that they have not seen yet, thus their headings are fixed in advance. If a value is
wider than its lane, the value runs over the lane and pushes the rest of the row to the
right. MeshTerm does not cut anything. The one field that has no limit on its width is
the last field, so it pushes nothing.

**No wrapping, with one exception.** A record is one line. Wrapping would put half of the
fields of a record under the wrong headings. Thus nothing folds, and a long line runs off
the right edge. The exception is the four written pages (`about`, `about-author`,
`discord`, `support`). These wrap at 72 cells. The no-wrap rule protects the fields of a
record. A paragraph has no fields, and without wrapping it is a line of 600 cells that
no terminal can read.

**Stdout is the answer. Everything else goes to stderr.** This includes errors
(`meshterm: what went wrong`), progress bars, log records, acknowledgements, and closing
messages:

```console
$ meshterm config set radio_sf 9
$ meshterm config set radio_sf 9 2>&1
✓ radio_sf = 9
✓ applied 1 change
```

An **acknowledgement** ("✓ device clock set") and the closing **message** of a command
("0 records stored") are for a person, and a script does not need them. They go to
stderr. Thus `meshterm contacts > contacts.txt` puts the contacts in the file and nothing
else, and the person at the prompt still sees the tick and the count.

---

## The JSON face

`--json` prints **the answer itself**. The caller still gets the report in `$?`.

**No envelope.** A listing is an array. A set of facts is an object. Thus the command
reads like the data that it returns:

```console
$ meshterm contacts --json | jq -r '.[] | select(.node.type == "repeater") | .node.key'
b2c3d4e500000000000000000000000000000000000000000000000000000000
a1b2c3d400000000000000000000000000000000000000000000000000000000
```

A command whose plain face prints two blocks (`trace`, `tx-optimize`) gives one object.
Each listing is under its own key (`edges`, `levels`), and the facts are merged at the top
level.

**One compact line, ended with a newline.** The text is UTF-8 with no ASCII escaping, so a
name in Cyrillic stays a name. Keys are in the order that the report declares them, and
MeshTerm never sorts them. Thus a document reads from the top down in the same order as
its plain face. `jq .` changes the indentation at no cost. Nothing can change a document
that is pretty-printed across 400 lines back into a stream.

**A stream is one document for each record, and all have the same shape.** `monitor` and
`chat listen` print newline-delimited JSON when each record arrives, and they flush the
output. There is no `begin` line, no `end` line, and no discriminator field. Each reader
would pay for a discriminator, and only a stream would use it. A capture survives
truncation: you lose the last line and not the file. Ctrl-C ends a run, and this is how
`--seconds 0` is designed to end. Such a run still keeps all that it heard.

**Absent is `null`, never an omitted key.** Each field that a command declares is in each
document. The field has `null` when the value is absent, unknown, or not applicable.
`null` is the counterpart of the `-` of the plain face, and you learn it once in the same
way. An empty collection is `[]` or `{}`. An empty string is `""`. It is a value, and it
is not the same as `null`. A whole object is `null` when its parts have no meaning apart:
`"position": null`, not `{"lat": null, "lon": null}`. Also, `unknown` becomes `null`. A
consumer already has one spelling for "nothing here".

**Values are typed.** Use `9`, not `"9"`. Use `false`, not `"false"`. The `yes` and `no`
of the plain face (`acked`, `applied`, `success`, `active`) are `true` and `false` here.
A message body is raw and not escaped, with its newlines, because a JSON string is not a
line.

**Timestamps are UTC, RFC 3339, `Z`, to the second.** An example is
`"2026-09-08T08:30:50Z"`. It has twenty characters, so a comparison of strings is a
comparison of times. The name of a field that holds a timestamp is `<something>_at`:
`heard_at`, `observed_at`, `created_at`, `scheduled_at`. **`--absolute` does not change
this.** A local offset is a fact about the machine that ran the command, and not about the
event. Two hosts that ask the same radio the same question must get the same document.

**A numeric field has its unit in its key.** Examples are `snr_db`, `rssi_dbm`,
`uptime_s`, `rtt_ms`, and `battery_v`. The unit is never in the value. A number has its
own sign (`5.1` and `-1.5`, never the `+5.1` of the plain column). **A key is never
truncated.** A truncated key cannot go back into `--to` or `--path`. Thus a short id is a
`hash` and has that name.

**`--json` changes the rendering and never the report.** The exit status is the same, and
the records are the same. An empty result prints its empty document and **still exits
5**:

```console
$ meshterm channels list --json
[]
$ echo $?
5
```

**"Prints nothing" is never a correct JSON answer.** A command whose plain face is silent
still says what it did:

```console
$ meshterm channels join 3 brigade 4ed883aaded6433c6606c9930a65044c
$ meshterm channels join 3 brigade 4ed883aaded6433c6606c9930a65044c --json
{"channel":{"slot":3,"name":"brigade","public":false,"hash":"5a"},"secret":"4ed883aaded6433c6606c9930a65044c","url":"meshcore://channel/add?name=brigade&secret=4ed883aaded6433c6606c9930a65044c"}
```

**A failure prints nothing on stdout.** The sentence stays on stderr, and the
classification stays in `$?`. There is no error document that you must tell apart from a
result. Also, `--json 2>/dev/null` still works correctly. Refer to
[When it goes wrong](#when-it-goes-wrong).

**A command with no data face refuses, and says so clearly.** `meshterm specimen --json`
is a usage error (exit 2). The output of `specimen` is the colour, and a document of it
would be false or without use. `meshterm emulate --json` is refused in the same way,
because `emulate` opens a window. `--json` with no subcommand is also refused, because an
interactive session cannot emit a document.

### The shapes that repeat

This section defines each shape one time. The tables for each command below use the names
and do not repeat the definitions.

**`node`** is the identity of a mesh participant, and nothing else. All five keys are
always present.

```json
{"name":"Yagi-Repeater","key":"a1b2c3d400000000000000000000000000000000000000000000000000000000","hash":"a1","type":"repeater","self":false}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string \| null | The name that the node advertises. It is never in quotation marks and never truncated. |
| `key` | string \| null | The full public key, in lowercase hex, 64 characters. `null` when MeshTerm heard only a hash. |
| `hash` | string \| null | The short id that comes from the key. Its width is the width that the command used to address the node. It is never a `key` that is cut short. |
| `type` | string \| null | `"companion"` \| `"repeater"` \| `"room server"` \| `"sensor"` \| `null`. |
| `self` | boolean | `true` for our node. This is what the star of the menu shows. The plain face deliberately does not show it. |

Reception facts belong to the **row** and not to the node. These facts are when MeshTerm
heard the node, how strong the signal was, and where the node is. They are different for
each command. A node object that carried them would mean a different thing in each
command. Thus a row holds `node` under its own key and does not flatten it. Then
`jq '.[].node.key'` reads the same on `contacts`, `courier`, and `monitor`.

> **Identity is thin when it comes from history.** A node that MeshTerm only overheard
> has `key: null` and `type: null`. Its `name` is what the resolver could find. To match
> nodes across commands, join on `hash` **and** the width that the command used to
> address it. Do not join on `key`.

**`position`** is `{"lat":45.5019,"lon":-73.5674}`. The values are decimal degrees as
stored, and they are not rounded. The five decimals of the plain face are a concession to
the width of the column. The whole object is `null` when the node has not shared a
location.

**`channel`** is `{"slot":0,"name":"Public","public":true,"hash":"11"}`. `slot` is the
value that the `INDEX` of each `channels` subcommand takes. `public` is the `TYPE` column
of the plain face as a boolean. **The secret is never in a `channel` object.** It appears
only when the caller asked for it, and then it has its own key.

**`setting`** is one device setting or one preference. `show` and `get` both use the
same shape.

```json
{"key":"radio_sf","value":8,"type":"int","label":null,"redacted":false}
```

`type` is `"int"` \| `"float"` \| `"bool"` \| `"str"` \| `"enum"`. `value` is the typed
current value. The value of an enum is its **number**, so `.value | tostring` is what
`set` takes as input. `label` is the word for a person for the number of an enum. For
other types, `label` is `null`. `redacted` is `true` when MeshTerm deliberately keeps the
value back. The `value` of a redacted row is `null`, because a mask string is not a
value. The rows of preferences also have `default` and `overridden`.

**`route` and `edges`**: a `route` is an array of `node` objects in the order of
propagation. Our node is a hop like each other hop. A `route` is `null` when nothing
came home, and never `[]`. An `edge` is one directed link,
`{"index":0,"from":{node},"to":{node},"snr_db":6.0}`. The SNR is the value that was
measured when the packet arrived at `to`. The plain face names the two ends of an edge by
hash, so that you can join the table of hops to the route line above it. JSON holds the
whole node at both ends. The join is part of the structure here, and each edge must be
readable on its own.

---

## Exit status

The exit status is half of the report, on both faces.

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | A failure that has no more specific code. Examples are an unreadable file, an unknown setting key or preference key (`config set`, `preferences set`), and an unhandled fault. |
| `2` | Usage error: an unknown flag, a missing argument, a value that the parser rejected, or a confirmation that you did not give. |
| `3` | MeshTerm could not select a companion device. None is attached, MeshTerm could not open the device that you named, or the choice was ambiguous. **Nothing was transmitted.** |
| `4` | MeshTerm reached a device, but the operation failed. Examples are a command error, a timeout, and a link that was lost during the run. It is reasonable to try again. |
| `5` | The command completed and there was **nothing to report**. Examples are an empty list, a target that never answered, and a conversation with no messages. This is not an error. |

`meshterm --help` also prints this table.

Two boundaries need an explanation. **When nothing was transmitted, the failure is not a
device failure.** If the tool refuses a value before it opens the radio, the error is a
usage error. Thus these cases are all `2`: a missing `--yes`, a bad `--sort` or
`--category`, an unknown `--profile`, an outbox id that does not exist, and `tx-optimize`
with no password available. Also, **a named connection that does not open gives `3`, not
`4`**. A `--port` that is not there means that MeshTerm selected no device, and nothing
went out.

Build your scripts on `5`. `grep` gives the same status for "found nothing" and for
"worked". A caller that must count the lines of the output to tell an empty mesh from a
full mesh parses the text when it can branch on the status:

```bash
if meshterm contacts > contacts.txt; then
    echo "$(($(wc -l < contacts.txt) - 1)) contacts"      # minus the header
elif [ $? -eq 5 ]; then
    echo "no contacts yet"
else
    echo "could not reach the radio" >&2
fi
```

`3` and `4` separate the failures that a new try can repair from the failures that it
cannot repair:

```bash
meshterm trace --target Alice
case $? in
  0) echo "reachable" ;;
  5) echo "no reply — the walk ran, nothing came home" ;;
  3) echo "no radio attached" >&2; exit 1 ;;
  4) echo "radio trouble — worth retrying" >&2; exit 1 ;;
  *) echo "failed" >&2; exit 1 ;;
esac
```

---

## When it goes wrong

MeshTerm **raises** a failure and never returns it. Nothing goes to stdout, one sentence
goes to stderr, and the classification goes to `$?`. This is the same on both faces.
`--json` does not put a failure in a document. A consumer that can see `$?` does not need
a document. A consumer that cannot see `$?` would have to tell an error document from a
result.

**No radio.** Nothing was transmitted. The exit status is `3` for each way to name the
device.

```console
$ meshterm --port NOSUCHPORT99 info
meshterm: could not open serial port NOSUCHPORT99: NOSUCHPORT99 isn't there — the device was unplugged, or came back under another name.
$ echo $?
3
```

The sentence names the cause and the remedy. For a serial port, the cause can be a port
that is missing, a port that another program uses, or a port that you cannot open. For
Bluetooth, the cause can be a link that never opened, a pairing that is out of date, or a
wrong PIN. [When a companion does not connect](../guide/connecting.md) lists all of these
causes.

If you name no device and more than one candidate is attached, the same `3` arrives with
a list and the way to correct the problem:

```console
$ meshterm info
meshterm: Multiple companion devices detected and no default to fall back on.
  • COM12 (serial) — USB Serial Device (COM12) [likely LoRa]
  • COM16 (serial) — USB Serial Device (COM16) [likely LoRa]
  • COM26 (serial) — USB Serial Device (COM26) [likely LoRa]
  • COM1 (serial) — Communications Port (COM1)
Choose one with --port <PORT> or --ble <ADDRESS> (or run 'meshterm devices' to inspect them). The chosen device is remembered as the default after it connects.
```

**A value that the parser rejects** gives `2`. The message names the choices, so you do
not have to find them:

```console
$ meshterm contacts --sort bogus
Usage: meshterm contacts [OPTIONS]
Try 'meshterm contacts --help' for help.

Error: Invalid value: --sort must be one of: name, heard, packets (got 'bogus')
```

**A confirmation that you did not give** is in the same class, because nothing has
happened yet:

```console
$ meshterm config reboot
Usage: meshterm config reboot [OPTIONS]
Try 'meshterm config reboot --help' for help.

Error: Invalid value: reboot restarts the device. Re-run with --yes to confirm.
$ echo $?
2
```

A destructive operation needs `--yes` and does not ask in a prompt. Thus nothing
unexpected happens in a script.

**A refusal** also gives `2`. The caller asked for something that the command does not
have:

```console
$ meshterm specimen --json
Usage: meshterm specimen [OPTIONS]
Try 'meshterm specimen --help' for help.

Error: Invalid value: specimen has no machine-readable output — its output is the colour
```

**The radio answered and the operation failed** gives `4`. The message says what to do
next:

```console
$ meshterm tx-optimize --path "Yagi-Repeater,Alice" --password wrong
logging in to Yagi-Repeater …
meshterm: admin login to 'Yagi-Repeater' failed (wrong password?). The saved password was cleared; re-run to enter a new one.
$ echo $?
4
```

**Nothing to report** gives `5`, and it is not a failure. Stdout is empty on the plain
face. With `--json`, stdout has the empty document. A message from MeshTerm about the
result (`0 records stored`) goes to stderr, like each other acknowledgement.

```console
$ meshterm records
$ echo $?
5
$ meshterm records --json
[]
```

With `--json`, none of this changes. The statuses are the same, and stderr is the same.
Stdout has the document for `0` and `5`, and it has nothing for `1` through `4`.

```console
$ meshterm contacts --sort bogus --json
Usage: meshterm contacts [OPTIONS]
Try 'meshterm contacts --help' for help.

Error: Invalid value: --sort must be one of: name, heard, packets (got 'bogus')
$ echo $?
2
```

An **unhandled fault** exits with `1`, and its traceback is in the log file. Each case
above is an expected failure, and MeshTerm reports it one time.

---

## Command reference

Each entry says what the command does, gives its options, and shows what the plain face
prints and what `--json` returns. If a JSON field is one of the [shapes that
repeat](#the-shapes-that-repeat), the table names the shape and does not define it again.

### Finding a device

#### `meshterm devices`

List each attached serial device, each radio on the SPI bus of this machine, and each
Bluetooth companion in range. The command opens no radio. It only lists what is attached
or what advertises. MeshTerm lists an SPI radio when its `/dev/spidev*` device file
exists. The device screen lists radios in the same way: one for each SPI profile, and
also the `/dev/spidev1.0` of the uConsole AIO when no profile covers it. With `--mock`,
the command finds no real device. The simulator is the one device, and the list shows it
with `--mock` as its target. MeshTerm does not touch the serial ports or the Bluetooth
adapter. A session that promised to use no real hardware keeps the promise here also.

| Option | What it does |
| --- | --- |
| `--ble` / `--no-ble` | Include a Bluetooth LE scan. It is on by default. If you skip it, the command is faster by a few seconds. It has no effect with `--mock`. |

```console
$ meshterm devices --no-ble
TARGET  TRANSPORT  NAME                        HARDWARE               MESHCORE  SERIAL            ACTIVE
COM12   serial     USB Serial Device (COM12)   Adafruit               maybe     D030D2CCD00F08EB  no
COM16   serial     USB Serial Device (COM16)   Adafruit               maybe     D8D7D4BB5E4546E0  no
COM26   serial     USB Serial Device (COM26)   Espressif              maybe     F85B1BA5EAD0      no
COM1    serial     Communications Port (COM1)  (Standard port types)  no        -                 no
```

`TARGET` is the value that `--port` and `--ble` take, exactly as shown. For an SPI radio,
`TARGET` is its device file. Reach the radio with `--spi` or with `-p` and its profile.
`MESHCORE` has three values and stays that way. `yes` is only for a device that a
connection proved to speak the protocol. `maybe` is for a USB vendor ID that suggests a
LoRa board or a bridge chip. `no` is for all other devices. A vendor ID is a hint, and
the column would be false if it changed a hint to a certain `yes`.

**`--json`** is an array of targets in the order of discovery. It has five facts that the
plain table has no room for.

```json
{"target":"COM12","transport":"serial","port":"COM12","address":null,"label":"USB Serial Device (COM12)","hardware":"Adafruit","meshcore":"maybe","confidence":"board","serial_number":"D030D2CCD00F08EB","stable_id":"sn:D030D2CCD00F08EB","confirmed":false,"remembered":false,"active":false}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `target` | string | The value that `--port` and `--ble` take, exactly as shown. |
| `transport` | string | `"serial"` \| `"ble"` \| `"tcp"`, or `"mock"` under `--mock`. |
| `port` | string \| null | The serial port, for a serial device. `""` on the `--mock` row. |
| `address` | string \| null | The Bluetooth address, for a Bluetooth device. |
| `label` | string | The name that the operating system gives to the device, or the confirmed node name when MeshTerm has one. |
| `hardware` | string \| null | The confirmed hardware model, or else the USB vendor label. `""` on the `--mock` row. |
| `meshcore` | string | `"yes"` \| `"maybe"` \| `"no"`. It is not a boolean, because a change of a hint to `true` would be false. |
| `confidence` | string | `"board"` \| `"bridge"` \| `"unknown"`. It shows the source of a `"maybe"`. |
| `serial_number` | string \| null | |
| `stable_id` | string | The identity that the device store uses as its key (`"sn:D030…"`, `"port:COM1"`). |
| `confirmed` | boolean | Whether MeshTerm stored this device as a proven companion. |
| `remembered` | boolean | Whether it is the remembered default. |
| `active` | boolean | Whether this run uses the device, or will use it. |

With `--mock`, there is no scan. The list is one row for the simulator: `target`
`"--mock"`, `transport` `"mock"`, `stable_id` `"mock:simulator"`.

The command returns `5` when it finds nothing.

---

### This node

#### `meshterm info`

The connected companion's live status: which radio it is, and how it works.

```console
$ meshterm info
name              MockCompanion
public_key        0000000000000000000000000000000000000000000000000000000000000000
role              companion
model             MeshCore Simulator
firmware          mock mock
battery_v         4.10
storage_used_kb   128
storage_total_kb  1024
clock_at          2026-09-08T04:24:41-04:00
clock_drift_s     -125  (2m 5s slow)
uptime_s          93784  (1d 2h)
noise_floor_dbm   -110
last_rssi_dbm     -62
last_snr_db       +9.5
tx_air_s          42  (42s)
rx_air_s          360  (6m)
packets_sent      210
packets_received  1234
receive_errors    3
```

The unit is in the key, so you do not have to find it in a sentence. A reading whose
number is a **duration** has an explanation beside it: `93784  (1d 2h)`. The number is
what the key promises and what a caller wants. The text in parentheses is what a person
wants to know.

`clock_at` is the device's clock. `clock_drift_s` is the device's clock minus the host's
clock, with a sign. On the plain face, `clock_at` is one of the instants
that stay as instants when the default is a relative age. A clock reading shows the time
that the device thinks it is, so the value `now` would tell nothing.

If the firmware does not answer for a reading, the reading is **absent**, and the plain
face does not print `-`. A missing key means that this device does not report the value.
A dash would mean that the device reported nothing.

**`--json`** is one object with the same keys, without the explanations in parentheses.
Each key is always present:

```json
{"name":"MockCompanion","public_key":"0000000000000000000000000000000000000000000000000000000000000000","role":"companion","model":"MeshCore Simulator","firmware":"mock mock","battery_v":4.1,"storage_used_kb":128,"storage_total_kb":1024,"clock_at":"2026-09-08T08:24:42Z","clock_drift_s":-125,"uptime_s":93784,"noise_floor_dbm":-110,"last_rssi_dbm":-62,"last_snr_db":9.5,"tx_air_s":42,"rx_air_s":360,"packets_sent":210,"packets_received":1234,"receive_errors":3}
```

`role` is `"companion"` \| `"repeater"` \| `"room server"` \| `"sensor"` \| `null`, or
`"type N"` for an advert type that MeshTerm does not know. This is the one place where
the two faces deliberately disagree about absence. The plain face omits a key that the
firmware never answered for. JSON writes `null`, because a set of keys that changes would
force each consumer to add a check before each field.

`info` does not hold a `node` object. This is our node, and `info` shows it in more
detail than the shape holds. `name` and `public_key` are its whole identity here. To find
what the radio is set to, use `config show`, not `info`. `info` never returns `5`.

#### `meshterm config`

Show and change each device setting. If you give no subcommand, it runs `show`.

| Subcommand | What it does |
| --- | --- |
| `show` | Print each setting as `key value`. |
| `get KEY` | Print the value of one setting, bare. |
| `set KEY VALUE` | Change one setting. |
| `backup PATH` | Write each setting to a TOML file. |
| `restore PATH [--dry-run]` | Apply the settings from a TOML backup. `--dry-run` prints the plan and changes nothing. |
| `custom KEY VALUE` | Set an experimental custom variable. |
| `channel INDEX NAME [--secret HEX]` | Configure a channel slot (refer also to the `channels` group). |
| `advert [--flood]` | Broadcast an advert. It is zero-hop unless you give `--flood`. |
| `share` | Print this node's contact card as a `meshcore://` URI. |
| `sync-clock` | Set the device clock from this computer. |
| `export-key [--out PATH]` | Export the private key. **Sensitive.** |
| `import-key KEY_HEX --yes` | Import a private key. It overwrites this node's identity. |
| `reboot --yes` | Reboot the device. |
| `factory-reset --yes` | Erase all data and reset the settings to the defaults. |

```console
$ meshterm config show
name                 MockCompanion
adv_lat              0.0
adv_lon              0.0
device_pin           ••••••
radio_freq           869.618
radio_bw             62.5
radio_sf             8
radio_cr             5
tx_power             20
client_repeat        false
airtime_factor       0.0
rx_delay             0.0
manual_add_contacts  false
autoadd_config       0
autoadd_max_hops     0
flood_scope          ""
adv_loc_policy       0
multi_acks           0
telemetry_mode_base  0
telemetry_mode_loc   0
telemetry_mode_env   0
path_hash_mode       0
```

`show` names each setting with the key that `get` and `set` take. It prints a value that
they accept as input: the **number** of an enum, not its label for a person, `""` for an
empty string, and `-` for a value that the firmware never reported. Thus you can type a
line from `show` back as input, and the write says what it replaced:

```console
$ meshterm config get radio_sf
8
$ meshterm config set radio_sf 9 --json
{"changes":1,"applied":[{"key":"radio_sf","previous":8,"value":9}]}
```

(If you read the value back with `config get` against real hardware, it answers `9`. The
simulator that these examples use opens a new radio for each run, so a setting that one
command writes is not there for the next command.)

The pairing PIN stays masked here, because `show` is the dump of the whole device. A
person can send this output to a file or paste it into a bug report. To get the PIN, ask
for it by name with `meshterm config get device_pin`. This is the one place where
MeshTerm does not withhold it.

Custom variables have the namespace `custom.*` in the output, because they have no spec
and `config set` does not accept them. Use `config custom`.

**`--json`.** `show` is an array of `setting` objects in the same order. `get` is one
`setting` object.

```console
$ meshterm config get tx_power --json
{"key":"tx_power","value":20,"type":"int","label":null,"redacted":false}
```

Three cases are separate. `flood_scope` is `""` (an empty value). `device_pin` is `null`
with `"redacted":true` (MeshTerm withholds it on purpose, and a mask string is not a
value). A setting that the firmware never reported is `null` with `"redacted":false`.

Each write prints what it did. The plain face is silent, and only the acknowledgement on
stderr says what happened:

```console
$ meshterm config set radio_sf 10 --json
{"changes":1,"applied":[{"key":"radio_sf","previous":8,"value":10}]}
$ meshterm config custom foo bar --json
{"changes":1,"applied":[{"key":"custom.foo","previous":null,"value":"bar"}]}
$ meshterm config channel 2 brigade --json
{"changes":1,"channel":{"slot":2,"name":"brigade","public":true,"hash":"b5"}}
$ meshterm config advert --json
{"sent":true,"flood":false}
$ meshterm config sync-clock --json
{"changes":1,"set_at":"2026-09-08T08:29:27Z","drift_s":-125}
$ meshterm config backup node.toml --json
{"path":"node.toml","settings":22,"channels":0,"custom":0}
$ meshterm config restore node.toml --dry-run --json
{"dry_run":true,"changes":0}
$ meshterm config export-key --json
{"private_key":"1111111111111111111111111111111111111111111111111111111111111111","path":null}
$ meshterm config reboot --yes --json
{"rebooted":true}
$ meshterm config share --json
{"url":"meshcore://contact/add?name=MockCompanion&public_key=0000000000000000000000000000000000000000000000000000000000000000&type=1","name":"MockCompanion","public_key":"0000000000000000000000000000000000000000000000000000000000000000","type":1}
```

`previous` is `null` when the snapshot held no earlier value. If the value already was the
value that you asked for, `changes` is `0` and `applied` is `[]`. The exit status stays
`0`, because the command did what you asked. The `path` of `backup` is the file that
MeshTerm wrote, as you named it. The plain face prints it bare, so a caller can keep it.
`export-key` has a private key, in the same way as the plain face. This is what you asked
the command to do, and MeshTerm adds no more gate here. `factory-reset --yes` answers
`{"factory_reset":true}`.

The menu draws a QR code beside `share`. This QR code has no JSON face and never will,
because it is a second rendering of `url`.

---

### The mesh around you

#### `meshterm contacts`

The contacts that your device knows.

| Option | What it does |
| --- | --- |
| `-s`, `--sort ORDER` | `heard` (default), `name`, or `packets`. |

```console
$ meshterm contacts --sort name
NAME            TYPE       HEARD  PKTS  HASH  LOCATION            KEY
Alice           companion     7m     -  d4    -                   d4e5f6a700000000000000000000000000000000000000000000000000000000
Local-Repeater  repeater      7m     -  b2    45.47680,-73.59900  b2c3d4e500000000000000000000000000000000000000000000000000000000
Observer-Bot    companion     7m     -  c3    45.48800,-73.58100  c3d4e5f600000000000000000000000000000000000000000000000000000000
Yagi-Repeater   repeater      7m     -  a1    45.50190,-73.56740  a1b2c3d400000000000000000000000000000000000000000000000000000000
```

`TYPE` is the node's advertised role in words: `companion`, `repeater`, `room server`,
`sensor`, or `unknown`. `HEARD` is a relative age, because a person opens this listing to
find out "what is recent?". `--absolute` changes it to an instant. `PKTS` is the number of
packets that passive monitoring heard.

**`HASH` is the token that `--path` and `--to` take.** Thus you do not have to cut it out
of `KEY` by hand. If you use the wrong width, you address a different node. `HASH` has
exactly the device's path hash width. **`LOCATION`** is a fact that the data always had,
but no column had room for it before. `KEY` stays complete and is the last column, so it
runs off the right edge without harm, and MeshTerm never cuts it. A caller cannot use a
truncated key as input.

This lists *contacts only*. Your node is not a contact. `meshterm info` reports it,
in more detail than a row can hold.

**`--json`** is an array of rows. Each row holds the `node` shape.

```json
{"node":{"name":"Observer-Bot","key":"c3d4e5f600000000000000000000000000000000000000000000000000000000","hash":"c3","type":"companion","self":false},"heard_at":"2026-09-08T08:30:50Z","packets":null,"position":{"lat":45.488,"lon":-73.581}}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `node` | object | The `node` shape. `hash` has the device's path hash width. |
| `heard_at` | timestamp \| null | When MeshTerm last heard the contact. `null` is the `never` of the plain face. |
| `packets` | integer \| null | The number of packets that passive monitoring heard. `null` when it heard none. This is not the same as `0`. |
| `position` | object \| null | The `position` shape. |

The command returns `5` and `[]` when the device does not know a contact yet.

#### `meshterm monitor`

Capture the packets that MeshTerm hears in the foreground, for a limited time. Then
summarize the capture. The command transmits nothing. It only listens. MeshTerm always
records to the history, with or without this command. This command adds the live view.

| Option | What it does |
| --- | --- |
| `-s`, `--seconds N` | How long to capture. `0` (the default) runs until Ctrl-C. |

```console
$ meshterm monitor --seconds 2
TIME                       NODE      SNR_DB  RSSI_DBM  LOCATION             SCOPE       NAME
2026-09-08T04:26:00-04:00  a1b2c3d4    +7.0      -101  45.50190,-73.56740   -           Yagi-Repeater
2026-09-08T04:26:00-04:00  b2c3d4e5    +9.5       -98  45.47680,-73.59900   -           Local-Repeater
2026-09-08T04:26:00-04:00  c3d4e5f6    +4.9       -93  -                    -           Observer-Bot
2026-09-08T04:26:00-04:00  d4e5f6a7    +7.4       -86  -                    -           Alice
NODE      NAME            PKTS  MEDIAN_SNR_DB  BEST_SNR_DB  RSSI_DBM  HEARD  LOCATION
d4e5f6a7  Alice            149           +6.9        +14.1       -93    now  -
c3d4e5f6  Observer-Bot     148           +5.7        +13.0      -108    now  -
b2c3d4e5  Local-Repeater   132           +6.7        +14.9      -102    now  45.47680,-73.59900
a1b2c3d4  Yagi-Repeater    132           +6.0        +13.6       -90    now  45.50190,-73.56740
```

The output has two blocks, one directly after the other. The summary starts at its own
`NODE` header line, and there is no blank line before it. The sample shows only the first
four stream records. The **stream** is a header and then one record for each packet when
the packet arrives. Its lanes are fixed in advance, because a capture cannot measure a
column that it has not seen yet. `TIME` is an absolute instant and not an age, because
each row of a live tail would read `now` otherwise. The **summary** gives the totals that
the stream cannot give: a count, a median, and a best value for each node. The node that
MeshTerm heard most recently is first.

**`--json` is the stream and only the stream.** It has one document for each packet, each
line has the same shape, and there is **no summary document at the end**:

```json
{"observed_at":"2026-09-08T08:30:40Z","node":{"name":"Yagi-Repeater","key":"a1b2c3d400000000000000000000000000000000000000000000000000000000","hash":"a1b2c3d4","type":"repeater","self":false},"kind":"advert","snr_db":7.0,"rssi_dbm":-101.3,"position":{"lat":45.5019,"lon":-73.5674},"scope":null,"path":null}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `observed_at` | timestamp | When the packet arrived. |
| `node` | object | The `node` shape. `hash` is the stored node id, four bytes wide. |
| `kind` | string | The packet class: `"advert"`, `"telemetry"`, `"packet"`, `"message"`, or `"ack"`. |
| `snr_db` | number \| null | |
| `rssi_dbm` | number \| null | |
| `position` | object \| null | The `position` shape. |
| `scope` | object \| null | The region that the flood was sent into: `{"state", "region", "code"}`. `state` is `"scoped"` (`region` names it), `"unknown"` (the flood is scoped, but no region that MeshTerm knows produces `code`), or `"unscoped"` (a plain flood). `code` is the frame's transport code in lowercase hex. MeshTerm keeps it also when it cannot find the region, so you can see that two packets share a region. `null` for a direct packet, or for a class whose route was never reported. A repeater never filters these by region. The plain `SCOPE` lane prints the region, `unknown`, `unscoped`, or `-`. |
| `path` | array of strings \| null | The relay chain that the packet arrived through. It has one lowercase-hex hash for each hop, in the order of propagation. `[]` is a direct reception. `null` means that the packet class has no path at all. This is the one place where `path` is an array, because it is a *received* relay chain and not a spec that you composed. |

**This is the one place where a whole block appears on one face and not on the other. It
is deliberate.** The summary of each node helps a person who watches a capture end. You
can calculate each figure in it from the records that scrolled past above it. If MeshTerm
emitted the summary as a second *shape* of line in the middle of a stream where all lines
are the same, each consumer would need a discriminator that it does not need otherwise.
Thus the totals stay on the plain face, and JSON gives you the records that the totals
come from:

```console
$ meshterm monitor --seconds 60 --json > capture.ndjson
$ jq -s -c 'group_by(.node.hash)[]
    | {hash: .[0].node.hash,
       name: (map(.node.name) | map(select(. != null)) | first),
       packets: length,
       best_snr_db: (map(.snr_db) | max)}' capture.ndjson
{"hash":"a1b2c3d4","name":"Yagi-Repeater","packets":32,"best_snr_db":12.8}
{"hash":"b2c3d4e5","name":"Local-Repeater","packets":32,"best_snr_db":14.9}
{"hash":"c3d4e5f6","name":"Observer-Bot","packets":36,"best_snr_db":12.2}
{"hash":"d4e5f6a7","name":"Alice","packets":36,"best_snr_db":13.0}
```

The "monitoring for 2s" announcement and the closing count go to stderr on both faces.
The command returns `5` when the window heard nothing.

#### `meshterm records`

The record-setting walks that MeshTerm scores each trace against. The command reads the
database. It needs no device and never transmits.

| Option | What it does |
| --- | --- |
| `-c`, `--category ID` | One discipline: `long_haul`, `far_point`, `long_leg`, `grand_tour`, `clean_trail`, `thin_thread`, `big_loop`. |
| `-w`, `--width N` | Only records that were set at this per-hop hash width (`1`, `2`, or `4`). |

The listing is `CATEGORY WIDTH SCORE UNIT RECORDED VERSION ROUTE`. `SCORE` is a bare
number, and `UNIT` is its own column, so you can compare the scores in a column. A score
with the prefix `>=` is a **lower bound**. This means that a hop on that walk has no known
position, so the distance is a minimum and not a measurement. MeshTerm keeps records for
each hash width. The width limits the maximum length of a walk and the chance of a
collision. Thus boards with different widths measure different games.

```console
$ meshterm records
$ echo $?
5
```

The walks in the menu find the records. Thus a database that only the command line used
has no records. This is what the simulator that these examples use prints, and it is
correct.

**`--json`** is an array that has two facts that the columns cannot hold.

| Field | Type | Meaning |
| --- | --- | --- |
| `category` | string | The discipline id, as `--category` takes it. |
| `hash_bytes` | integer | The per-hop hash width at which MeshTerm transmitted the walk. It is the `WIDTH` of the plain face, with the contract's word for the concept. |
| `score` | number | The bare score, without a qualifier. |
| `unit` | string | `"km"` \| `"nodes"` \| `"dB"` \| `"km²"`. |
| `lower_bound` | boolean | **JSON only.** `true` when the score is a minimum. It is the `>=` prefix of the plain face, extracted. Thus a consumer that compares scores does not have to remove a qualifier from the front of its own data. |
| `recorded_at` | timestamp | |
| `version` | string | The MeshTerm version that found the record. |
| `path` | string \| null | **JSON only.** The spec that MeshTerm transmitted, in comma-separated hex. A caller uses it to walk the path again. It has no column, because a route line already runs off the right edge. `null` when the record has no stored spec. |
| `route` | array | The `route` shape, with hops that match `path`. |

The command returns `5` and `[]` when no records match.

---

### Messaging

#### `meshterm chat`

`meshterm chat` on its own is not interactive. It prints the help of this group and exits
with `2`. The live transcript is a menu screen. On the command line, select a subcommand.

| Subcommand | What it does |
| --- | --- |
| `send TEXT --to NAME` | Send a direct message, or post to a room. |
| `send TEXT --channel N [--scope REGION]` | Broadcast on a channel slot, with the channel's scope. For this one message, you can use `REGION` as the scope, or `*` for unscoped. |
| `history [--to NAME \| --channel N] [--limit N]` | Print the stored transcript of a conversation. The default limit is 50. |
| `list` | Each channel, room, and contact, with its unread count and last message. |
| `listen [-s SECONDS] [--debug]` | Tail the inbound messages live. `-s 0` (the default) runs until Ctrl-C. |

`send` and `history` need exactly one of `--to` and `--channel`.

```console
$ meshterm chat send "on my way" --to Alice
acked  yes
```

A direct message prints its `acked` state. This is the one fact that the send does not
already tell you. The radio accepts the message in each case, and whether the peer
answered is a separate fact. A **channel** broadcast prints nothing. There is no
acknowledgement on a channel, so the exit status is the whole answer.

```console
$ meshterm chat history --to Alice
TIME  DIR  PEER   SNR_DB  TEXT
 now  out  Alice       -  on my way
 now  out  Alice       -  see you there

$ meshterm --absolute chat history --to Alice
                     TIME  DIR  PEER   SNR_DB  TEXT
2026-09-08T04:27:37-04:00  out  Alice       -  on my way
2026-09-08T04:27:38-04:00  out  Alice       -  see you there
```

`DIR` is `in` or `out`. `PEER` is always the *other* party. `TEXT` is the last column and
takes the rest of the line. A message body is the one field that can hold any text, so no
column can follow it.

**Rooms.** A room server is a message board that lives on a radio. First, join the room
with [`meshterm rooms join`](#meshterm-rooms). After that, post with `send --to`, in the
same way as a direct message. `acked yes` means that the room stored the post. It does
not mean that a person read it. The `history` of a room names the author of each post.
The `history` of a direct conversation names the peer:

```console
$ meshterm chat history --to "Lakeside BBS"
TIME  DIR  AUTHOR                    SNR_DB  TEXT
 12m  in   Alice (d4e5f6a7)            +6.5  Anyone driving to the swap meet Saturday?
  5m  in   e5f6a7b8                    +2.0  Is the north repeater down?
 now  out  MockCompanion (00000000)       -  I'll check it tonight
```

An author is a name with the hash of its key. If nothing that you heard names that key,
the author is only the hash. Your own posts name your node.

```console
$ meshterm chat list
CONVERSATION    KIND     UNREAD   LAST  LAST_TEXT
Public          channel       0  never  -
Yagi-Repeater   direct        0  never  -
Local-Repeater  direct        0  never  -
Observer-Bot    direct        0  never  -
Alice           direct        0  never  -
Lakeside BBS    room          0  never  -
```

```console
$ meshterm chat listen -s 3
TIME                       PEER              SNR_DB  TEXT
2026-09-08T04:30:44-04:00  b2c3d4e5            +4.9  hello from Local-Repeater #1
2026-09-08T04:30:44-04:00  c3d4e5f6            -0.6  hello from Observer-Bot #2
2026-09-08T04:30:45-04:00  d4e5f6a7            +3.9  hello from Alice #3
```

`listen` fixes its lanes and puts an instant on each row. The stream of `monitor` has the
same shape, for the same reason. `--debug` sends the log of the message pull to stderr.
Use it to find out if MeshTerm pulls messages from the companion at all.

**`--json`.** `send` reports what it sent, and to whom:

```console
$ meshterm chat send "see you there" --to Alice --json
{"kind":"direct","node":{"name":"Alice","key":"d4e5f6a700000000000000000000000000000000000000000000000000000000","hash":"d4e5f6a7","type":"companion","self":false},"channel":null,"sent":true,"acked":true}

$ meshterm chat send "net in 5" --channel 0 --json
{"kind":"channel","node":null,"channel":{"slot":0,"name":"#0","public":true,"hash":null},"sent":true,"acked":null}
```

`kind` is `"direct"` \| `"room"` \| `"channel"`. It shows which of `node` and `channel`
has a value (a room is a `node`).

A channel send also has `scope`, in the same shape that `monitor` gives for a received
packet. For a region, it is `{"state":"scoped","region":"harbour","code":null}`. For a
plain flood, it is `"unscoped"`. When the scope is not known, it is `null`. This happens
for a direct message, and for a channel that has no scope of its own when MeshTerm could
not read the device's default. `code` is `null` on a send, because the code is a hash of
the packet, and the radio builds the packet.

`--scope` applies to a channel send only. A direct message has no scope of its own. The
radio floods it with the device's default, in the same way as other messages. Thus
`--scope` with `--to` is a usage error (exit `2`). A name that the firmware would refuse
is also a usage error. A radio that cannot send with the scope (firmware older than
1.10, or `*` before 1.16 with a default scope set) refuses before it transmits anything.
This is a failure (exit `1`). MeshTerm never sends the message unscoped without telling
you. Put the `*` in quotation marks, so that the shell does not change it:

```console
$ meshterm chat send "all clear" --channel 0 --scope '*' --json
{"kind":"channel","node":null,"channel":{"slot":0,"name":"#0","public":true,"hash":null},"sent":true,"acked":null,"scope":{"state":"unscoped","region":null,"code":null}}
```

`acked` is `null` on a channel, not `false`. A channel has no acknowledgement, and this is
the purpose of the one absence token. It is also the reason why the plain face prints
nothing there: it has no way to say it.

`history` is an array, with the oldest message first:

```json
{"created_at":"2026-09-08T08:27:37Z","direction":"out","node":{"name":"Alice","key":null,"hash":"d4e5f6a7","type":null,"self":false},"channel":null,"author":null,"snr_db":null,"text":"on my way","acked":true}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `created_at` | timestamp | |
| `direction` | string | `"in"` \| `"out"`. |
| `node` | object \| null | The *other* party, for a direct conversation. |
| `channel` | object \| null | The `channel` shape, for a channel conversation. |
| `author` | object \| null | The author of a room post, as a `node`. On your own posts, it is your node. `null` outside a room. |
| `snr_db` | number \| null | The SNR at reception. `null` on an outbound message. |
| `text` | string | The body, raw and not escaped. |
| `acked` | boolean \| null | The delivery state of an outbound direct message. `null` for an inbound message and for a channel. |

`list` is an array of conversations, with the channels first. Each conversation has
`kind`, `channel`, `node`, `unread`, `last_message_at`, and `last_text`. `listen` is a
stream that has the same row shape as `history`, but it has **`received_at`** instead of
`created_at`:

```json
{"received_at":"2026-09-08T08:30:47Z","node":{"name":null,"key":null,"hash":"b2c3d4e5","type":null,"self":false},"channel":null,"author":null,"snr_db":4.9,"text":"hello from Local-Repeater #1","direction":"in","acked":null}
```

A room post arrives from the room, so its `node` is the room. Its `author` has the
author's hash.

An inbound message has only the sender's key prefix. Thus its `node` has less data than
the same node in `contacts`. `name` is what the resolver could find, and `key` is `null`.
Join on `hash`.

`history`, `list`, and `listen` return `5` when there is nothing to report.

#### `meshterm rooms`

A room server is a message board that lives on a radio. It keeps the latest posts. After
you log in, it sends you each post that you missed, and then each new post when a person
writes it. To join a room, you log in with its password. You get the password from the
person who runs the room. `meshterm rooms` on its own prints the help of this group. The
Rooms page in the menu does the same jobs with dialogs.

| Subcommand | What it does |
| --- | --- |
| `list` | Each room server that your radio knows, joined or not. |
| `join ROOM [--password PASSWORD] [--flood]` | Log in, and print what the room lets you do. |
| `forget ROOM` | Stop the login to a room, and forget its password. |

```console
$ meshterm rooms list
ROOM          ACCESS  HEARD  LAST_POST  UNREAD
Lakeside BBS  member    12m         3m       0

$ meshterm rooms join "Lakeside BBS" --password hello
member
```

`join` prints what the room let you do. `member` means that you can read and post.
`admin` means that you are the room's owner. `read-only` means that the room does not
keep what you post. MeshTerm remembers a password that works, so the next `join` does not
need `--password`. An empty password (`--password ""`) asks the room if it already knows
you. MeshCore sends a maximum of 15 characters of a password. MeshTerm refuses a longer
password before it sends anything (exit `2`).

A room never says that a password is wrong. It does not answer. Thus a `join` that hears
nothing fails with exit `4`. The message says what MeshTerm knows: how the login went
out, when MeshTerm last heard the room, and if this password worked before:

```console
$ meshterm rooms join "Lakeside BBS" --password nope
meshterm: Lakeside BBS didn't answer. It went along the route your radio learned to the
room (1 hop), and a route goes stale when the mesh changes. The room was heard 12m ago. A
room doesn't answer a wrong password — it stays silent — so either it didn't hear you, or
the password is wrong. Try --flood.
```

Your radio sends a login along the route that it learned the last time that the room
answered. If that route is stale, the login is lost on the way. `--flood` makes the radio
forget the route first. Then the login goes to the whole mesh, and the room's answer
teaches the radio a new route. When the radio forgets the route, it sends nothing. The
login is still one transmission.

The posts that a room sends after a login wait on your radio until something collects
them. `chat listen` or Chat in the menu collects them. `chat history --to ROOM` reads
them by author.

`forget` is local and prints nothing. MeshCore has no way to leave a room. Thus the room
can continue to send new posts to your radio until the room restarts, or until it needs
your seat for a new member (it holds 20 members, and it removes the least active member).
`forget` exits with `5` if you did not join the room.

**`--json`.** `list` is an array, one object per room:

```json
{"node":{"name":"Lakeside BBS","key":"f6a7b8c900000000000000000000000000000000000000000000000000000000","hash":"f6a7b8c9","type":"room server","self":false},"joined":true,"access":"member","heard_at":null,"last_post_at":null,"unread":0}
```

`access` is `null` for a room that you did not join. `heard_at` is `null` for a room that
MeshTerm never heard. `join` answers with the room, the access, and how the login went
out. `route` is `"flood"`, `"direct"`, or `null` if the radio never confirmed that it
sent the login:

```json
{"room":{"name":"Lakeside BBS","key":"f6a7b8c900000000000000000000000000000000000000000000000000000000","hash":"f6a7b8c9","type":"room server","self":false},"access":"member","route":"flood"}
```

`forget` answers with the room and `forgotten` (`true`, or `false` when you did not join
the room).

#### `meshterm channels`

| Subcommand | What it does |
| --- | --- |
| `list` | The configured channel slots. |
| `add INDEX NAME [--secret HEX]` | Add a channel. A `#` at the start of the name makes the channel public (the firmware derives the key from the name). Otherwise the channel is private, with a random key unless you give a key. |
| `join INDEX NAME SECRET` | Join a channel from its name and its key of 32 hex characters. |
| `import INDEX URL` | Import a `meshcore://channel/add` link. |
| `import --file FILE [--dry-run]` | Add the channels of a file from `export` to the device. |
| `export FILE` | Write each channel to a file, with its key, its send scope, and its mute. Another device can import the file. |
| `guide` | The public channels that MeshTerm heard, with the number of messages and the age of the last one. |
| `share INDEX` | Print the share link of a slot. |
| `clear INDEX --yes` | Clear a slot. This removes the channel from the device. |
| `scope INDEX [REGION] [--clear]` | Show or set the region into which the channel's messages are flooded. |

`list` prints `SLOT NAME TYPE HASH SCOPE`, with one record for each configured slot. It
returns `5` when the device has no configured slot. The simulator that these examples use
has none, so there is no captured listing to show. Each `--mock` run opens a new radio,
and a slot that one run writes is not there for the next run.

```console
$ meshterm channels add 1 brigade
meshcore://channel/add?name=brigade&secret=2c70ca416304d520b60a28d9db35d6b3
```

`add` and `share` print the `meshcore://` URI alone, because the caller asked for
something to pass on. `join` and `import` print nothing. You gave these two commands the
key, so it is not an answer to print it back to you. The menu draws a QR code beside the
link. If you send that output to a file, the file has a block of block characters around
the one thing that is the answer.

`clear` needs `--yes`, because MeshTerm loses the key of a private channel with the slot,
unless you saved the key somewhere else.

MeshTerm keeps `scope`, not the radio. The firmware has no scope for each channel, so
MeshTerm sets the companion's scope around each message that it sends on the channel.
Thus a `scope` that you set writes nothing to the device, and it follows the channel to
each slot that the channel moves to. If you give no `REGION`, `scope` prints only the
channel's region, in the same way that `config get` prints one value. A channel that has
no scope prints `-` (`null` in `--json`) and exits with `0`. This is an answer: the
channel sends with the device's default. To set a scope (`scope 1 harbour`) or to clear
it (`scope 1 --clear`), the plain face prints nothing. If you read an empty slot, the
exit status is `5`, as for `share`. If you write to an empty slot, it is a usage error
(exit `2`), because there is no channel to scope. `SCOPE` in `list` is `-` for a channel
that sends with the device's default.

**`--json`.** `list` is an array with **flat** rows. This is the one listing whose record
*is* the shared shape, and does not contain it. A row is
`{"slot":0,"name":"Public","type":"public","hash":"11","scope":null}`. `type` is the word
of the plain column, and it is not the `public` boolean of the `channel` shape. If each
row had a `channel` key, `jq '.[].channel.name'` would be necessary for a listing in
which each column is already the channel.

All four writing subcommands emit the same document, and each one has the key that it
wrote:

```console
$ meshterm channels add 2 "#news" --json
{"channel":{"slot":2,"name":"#news","public":true,"hash":"03"},"secret":"ecadb1a7d803db8958bea1302ca6e8be","url":"meshcore://channel/add?name=%23news&secret=ecadb1a7d803db8958bea1302ca6e8be"}

$ meshterm channels clear 1 --yes --json
{"slot":1,"cleared":false}

$ meshterm channels share 1 --json
{"channel":null,"secret":null,"url":null}
```

`share` on an empty slot, and `clear` on a slot that is already empty, give exit `5` with
the nulls and `"cleared":false`. There is nothing to report, and it is not a failure.

**Export and import.** `export` and `import --file` move all the channels to another
device. `export` writes one TOML file, with one `[[channels]]` table for each channel, in
slot order:

```toml
[[channels]]
name = "#news"

[[channels]]
name = "brigade"
secret = "2c70ca416304d520b60a28d9db35d6b3"
scope = "harbour"
muted = true
```

A `#` channel has no `secret`, because the device derives its key from the name. A private
channel has its key in the file. Thus the file is as secret as the keys in it. MeshTerm
makes it readable only by you, where the system lets it, and `export` says on stderr how
many keys of private channels the file holds. A file from `config backup` has a
`[[channels]]` table of the same shape, so `import --file` can also read a config backup.

`import --file` never removes a channel. It puts the channels of the file first, in the
order of the file, and the other channels of the device after them. A channel that has no
free slot is not added. The send scope and the mute of each channel go into the stores of
this computer. A channel that has no scope or no mute in the file keeps the values that it
has here. `--dry-run` prints the plan and changes nothing.

```console
$ meshterm channels import --file brigade.toml
SLOT  NAME     ACTION
   0  #news    add
   1  brigade  add
```

`import --file` prints the layout of the device after the import, with one record for each
channel. `ACTION` is `add`, `move`, or `keep`. A channel that has no free slot comes last,
with the action `skip` and no slot. `export` prints only the path of the file. It writes no
file and exits with `5` when the device has no channel. A file that MeshTerm cannot use is
a usage error (exit `2`), and the message names the bad entry. If MeshTerm cannot read
each slot of the device, the command changes nothing and exits with `4`.

```console
$ meshterm channels import --file brigade.toml --json
{"dry_run":false,"added":2,"moved":0,"skipped":0,"channels":[{"slot":0,"name":"#news","action":"add"},{"slot":1,"name":"brigade","action":"add"}]}

$ meshterm channels export channels.toml --json
{"path":null,"channels":0,"private":0}
```

**The guide.** `guide` lists each public channel that MeshTerm heard, the most recently
heard first. MeshTerm stores each channel packet that it hears, also on a channel that is
not on your device. A packet gives its channel only as a hash, so MeshTerm tries names:
the channels of your device, `Public`, each `#name` in a stored message, and each region
that it knows. The key of a public channel comes from its name, and MeshTerm checks the MAC
of each packet with that key, as the firmware does. A guessed name must match two messages
or more, because one match of three bytes is not proof. A private channel, or a public
channel whose name nobody wrote, has no name in the guide. stderr gives the number of
these messages.

```console
$ meshterm channels guide
NAME      HASH  MSGS  LAST  DEVICE
#news     03       2   now  yes
#harbour  26       3   now  no
```

`MSGS` counts different messages: the copies that repeaters relayed count one time.
`DEVICE` says whether the device has the channel. To add a channel from the guide, give
its name to `add` (`channels add 2 "#harbour"`). When MeshTerm heard no public channel,
`guide` exits with `5`. Under `--json`, each record is
`{"name":"#news","hash":"03","messages":2,"last_heard":"2026-10-07T05:23:09Z","on_device":true}`.

#### `meshterm courier`

A store-and-forward outbox. Queue a message for a contact that you cannot reach now. The
message goes out when MeshTerm next hears the contact, or at a time that you name.

| Subcommand | What it does |
| --- | --- |
| `queue CONTACT TEXT… [--at HH:MM]` | Queue a message. `--at` holds it until the next time that the local clock shows that time. |
| `list` | The outbox: the waiting entries, then the finished entries. |
| `send ID` | Force one delivery attempt now. It skips the "wait until heard" check. |
| `cancel ID` | Remove a waiting entry. |
| `clear` | Remove each finished entry (delivered or given up). |

```console
$ meshterm courier queue Alice "call me when you land" --at 18:30
id  1

$ meshterm courier list
ID  STATE    NODE   SCHEDULED                  FINISHED  TEXT
 1  waiting  Alice  2026-09-08T18:30:00-04:00         -  call me when you land
```

`queue` prints the entry's **id**. This is the one fact that you must keep, because
`send` and `cancel` take it. An entry with no `SCHEDULED` time goes when MeshTerm hears
the contact.

`SCHEDULED` and `FINISHED` stay **absolute, also without `--absolute`**. An appointment is
one of the places where the instant is the fact. "18:30" is what you asked for and what
will happen. "In 14h" says the same thing again, and it is wrong after a short time.

The courier of a running interactive session does the *sending*. A queued message stays
in the outbox until such a session runs. `courier send ID` is the way to force an attempt
from a script.

**`--json`.**

```console
$ meshterm courier list --json
[{"id":1,"state":"waiting","node":{"name":"Alice","key":null,"hash":"d4e5f6a70000","type":null,"self":false},"scheduled_at":"2026-09-08T22:30:00Z","finished_at":null,"text":"call me when you land"}]

$ meshterm courier cancel 1 --json
{"id":1,"cancelled":true}

$ meshterm courier clear --json
{"cleared":0}
```

`state` is `"waiting"` \| `"delivered"` \| `"gave up"`. `queue` answers with `id`, `node`,
`scheduled_at`, and `text`. `send ID` answers `{"id":…,"outcome":…}`. `outcome` is one of
`delivered`, `no ack`, `gave up`, `unknown contact`, `busy`, or `gone`. MeshTerm keeps
each value exactly as it is, with the spaces, so the two faces use the same word.

`list` returns `5` for an empty outbox. `cancel` and `clear` return `5` when there was
nothing to act on. These cases are **usage errors** (`2`): a contact that the device does
not know, a contact with no key to address, and an `--at` that is not a clock time. The
argument is wrong, nothing was queued, and nothing went out over the air.

---

### Tracing

#### `meshterm trace`

Walk the path to a target one time and report what came back.

| Option | What it does |
| --- | --- |
| `-t`, `--target NAME` | **Required.** The name or the key prefix of the target node. |
| `--path SPEC` | Force a path. Give comma-separated contact names or hex hashes, in any mix (`3d,f2,3d`). If you omit it, the device routes. |

`--target` is required, so `meshterm trace --path …` alone is a usage error. The form
without a target is [`trace-path`](#meshterm-trace-path).

```console
$ meshterm trace --target Alice --path "a1,d4"
target      Alice
path        a1,d4
success     yes
hops        3
min_snr_db  +2.0
rtt_ms      123
route       MockCompanion (00) → Yagi-Repeater (a1) → Alice (d4) → MockCompanion (00)

HOP  FROM  TO  SNR_DB
  0  00    a1    +6.0
  1  a1    d4    +2.0
  2  d4    00    +6.0
```

The output has two blocks. The first block shows what the walk did. It has the drawn
`route` among the facts, and the typed `path` beside it. `path` reads `auto` when the
device routed. Each other value there is the comma-separated hex that you can paste into
`--path` as it is. The second block has one record for each hop. It names the two ends of
the hop by **hash** and does not repeat the names. The names are in the route line above,
and the hash joins the two blocks.

A trace that never came home is **not** a failure of the command. The radio transmitted,
and the walk ran. The command reports `success no` and returns `5`.

#### `meshterm trace-path`

The other half of tracing. It has no target. You compose a path by hand, out and back, in
any way that you choose. The path only has to end where this node can hear it.

| Option | What it does |
| --- | --- |
| `--path SPEC` | **Required.** The whole walk, comma-separated. |

```console
$ meshterm trace-path --path "a1,d4,a1"
target      -
path        a1,d4,a1
success     yes
hops        4
min_snr_db  +2.0
rtt_ms      458
route       MockCompanion (00) → Yagi-Repeater (a1) → Alice (d4) → Yagi-Repeater (a1) → MockCompanion (00)

HOP  FROM  TO  SNR_DB
  0  00    a1    +6.0
  1  a1    d4    +2.0
  2  d4    a1    +2.4
  3  a1    00    +6.0
```

`target` is `-` because a path walk has none. MeshTerm records these walks under a
placeholder target, so they stay out of the history of the target picker.

**`--json`** is one object for both commands. The facts are at the top level, and the
table of hops is under `edges`.

| Field | Type | Meaning |
| --- | --- | --- |
| `target` | string \| null | The label that the trace was addressed to. `null` for a path walk. |
| `path` | string \| null | The forced spec as hex, or `null` when the device routed (the `auto` of the plain face). |
| `success` | boolean | Whether a reply came home. |
| `hops` | integer \| null | |
| `min_snr_db` | number \| null | The SNR of the bottleneck hop. |
| `rtt_ms` | number \| null | |
| `tx_dbm` | integer \| null | The TX power when the trace ran, if known. **No plain column**, because the facts block has no room. |
| `hash_bytes` | integer \| null | The per-hop hash width that the walk used to address nodes. Present when the path was forced. |
| `route` | array \| null | The `route` shape. `null` when nothing came home. |
| `edges` | array | The `edge` shape, one for each hop, in the order of the walk. `[]` when nothing came home. |

```console
$ meshterm trace-path --path "a1,d4" --json | jq -r '.edges | min_by(.snr_db) | "\(.from.hash) -> \(.to.hash)  \(.snr_db) dB"'
a1 -> d4  2.0 dB
```

#### `meshterm tx-optimize`

Sweep the transmit power of a remote node against a target. Then select the lowest level
that still gets through reliably.

| Option | What it does |
| --- | --- |
| `--path SPEC` | **Required.** A forced path that ends at the target (`Repeater,Target`, or `3d,f2`). The hop before the target is the node that MeshTerm tunes. |
| `-n`, `--samples N` | The number of traces for each TX level. The default is 3. |
| `--step N` | The step of the coarse sweep. The default is 3. |
| `--min N` / `--max N` | Set the limits of the sweep. If you omit them, MeshTerm uses the `tx_opt_min` and `tx_opt_max` preferences. |
| `--password TEXT` | The admin password of the tuned node. If you omit it, MeshTerm uses the remembered password, or else asks for it. |
| `--apply` / `--no-apply` | Set the winner on the node. It is on by default. |

```console
$ meshterm tx-optimize --path "Yagi-Repeater,Alice" --samples 2 --password admin
tuning_node      Yagi-Repeater
target           Alice
path             a1b2c3d4,d4e5f6a7
optimal_tx_dbm   19
target_snr_db    +8.7
reliability      1.00
previous_tx_dbm  20
applied          yes

TX_DBM  TARGET_SNR_DB  SUCCESSES  SAMPLES
    18           +6.6          2        2
    19           +8.7          4        4
    20           +9.1          2        2
    21           +7.6          2        2
    24           +4.0          2        2
    27           -9.9          2        2
    28          -15.1          1        2
```

The winner is first, because it is what you asked for. The records for each level follow,
so that you can check the choice against the measurements that it came from. This command
transmits many times, with the wait that the `trace_cooldown_s` preference sets. It is the
one command that is not a single transmission.

**`--json`** puts the levels under `levels`. It also changes the two bare names to `node`
objects. This is most important here, because nothing else on the line shows which node
is which.

```json
{"tuning_node":{"name":"Yagi-Repeater","key":"a1b2c3d400000000000000000000000000000000000000000000000000000000","hash":"a1b2c3d4","type":"repeater","self":false},"target":{"name":"Alice","key":"d4e5f6a700000000000000000000000000000000000000000000000000000000","hash":"d4e5f6a7","type":"companion","self":false},"path":"a1b2c3d4,d4e5f6a7","optimal_tx_dbm":19,"target_snr_db":8.7,"reliability":1.0,"previous_tx_dbm":20,"applied":true,"levels":[{"tx_dbm":18,"target_snr_db":6.6,"successes":2,"samples":2},{"tx_dbm":19,"target_snr_db":8.7,"successes":4,"samples":4},{"tx_dbm":20,"target_snr_db":9.1,"successes":2,"samples":2},{"tx_dbm":21,"target_snr_db":7.6,"successes":2,"samples":2},{"tx_dbm":24,"target_snr_db":4.0,"successes":2,"samples":2},{"tx_dbm":27,"target_snr_db":-9.9,"successes":2,"samples":2},{"tx_dbm":28,"target_snr_db":-15.1,"successes":1,"samples":2}]}
```

`reliability` is a fraction in `[0, 1]`, not a formatted `"1.00"`. `levels` is in
ascending order of `tx_dbm`, as the plain table is. The star of the menu on the winning
row has no column here, because `optimal_tx_dbm` names the winner.

If no password is available and MeshTerm cannot ask for one, this is a **usage error**
(`2`), and nothing was transmitted. If the node refuses the password, this is a device
error (`4`), and MeshTerm clears the saved password. The command returns `5` when no
trace reached the target at any level. In this case MeshTerm measured nothing, and the
node stays at its original power.

---

### Other nodes

#### `meshterm repeater-admin`

Send one command to a remote repeater or room server over the mesh, and print its reply.

```
meshterm repeater-admin NODE COMMAND... [--password TEXT]
```

| Argument / option | What it does |
| --- | --- |
| `NODE` | The name of the remote contact. |
| `COMMAND...` | The text command to send, spelled as the node's own CLI spells it. |
| `--password TEXT` | The admin password. If you omit it, MeshTerm uses the password that it remembers from an interactive login. |

```console
$ meshterm repeater-admin Yagi-Repeater get name --password admin
> Yagi-Repeater
```

The command prints the reply as the node sent it: the node's own text, the whole answer,
with the echoed prompt. A node that does not answer is not a failure, because the command
can have arrived. But there is nothing to report, so the command returns `5`.

MeshTerm logs in automatically with the remembered password. To store a password, run
the interactive flow one time, or give `--password`. If the node refuses the login,
MeshTerm clears the saved password, and the command fails with `4`. The node answered,
and the answer was no. A password that is **absent**, and a node name that no contact
matches, are usage errors (`2`). In both cases nothing was transmitted. This is the line
between these errors and the errors `3` and `4`.

**`--json`:**

```json
{"node":{"name":"Yagi-Repeater","key":"a1b2c3d400000000000000000000000000000000000000000000000000000000","hash":"a1b2c3d4","type":"repeater","self":false},"command":"get name","reply":"> Yagi-Repeater"}
```

`reply` is the node's own text, whole and exactly as received. It is raw and not escaped,
and it has more than one line when the node sent more than one line. MeshTerm does **not**
parse it. MeshTerm does not know the grammar of the remote node's CLI, and a document that
pretended to know it would invent a structure. `reply` is `null` when the node gives no
answer (exit `5`).

#### `meshterm regions`

Ask a repeater for which regions it relays floods (firmware 1.12 or newer).

```
meshterm regions NODE
```

| Argument / option | What it does |
| --- | --- |
| `NODE` | The contact name of the repeater. |

```console
$ meshterm regions Yagi-Repeater
node      Yagi-Repeater
unscoped  yes
regions   lakeside, lakeside-north, harbour
```

The command sends one anonymous request, with no login, and it is one transmission. A
repeater answers the request only when the request arrives direct, from a neighbour or
over a route that the companion learned. The repeater also limits the rate of this
question, so the command does not try again. `unscoped` says if the repeater also relays
plain floods. `regions` lists the regions for which it relays *scoped* floods. MeshTerm
remembers the answer, so the node page and the packet views know it afterwards.

A repeater that answered and named no region relays no floods, and the command returns
`5`. A repeater that never answered gives a device error (`4`). These cases are usage
errors (`2`) and MeshTerm sent nothing: a name that no contact matches, and a contact that
is known to be something other than a repeater.

**`--json`:**

```json
{"node":{"name":"Yagi-Repeater","key":"a1b2c3d400000000000000000000000000000000000000000000000000000000","hash":"a1b2c3d4","type":"repeater","self":false},"unscoped":true,"regions":["lakeside","lakeside-north","harbour"]}
```

`regions` never holds the wildcard `*`. That is `unscoped`, because it names no region.

---

### MeshTerm itself

#### `meshterm preferences`

How MeshTerm behaves. This is different from how the radio is configured. The command
reads nothing from the companion, transmits nothing, and works with no device attached.
If you give no subcommand, it runs `show`.

| Subcommand | What it does |
| --- | --- |
| `show` | Each preference, with its value, its built-in default, and what it does. |
| `get KEY` | The value of one preference, bare. |
| `set KEY VALUE` | Change one preference. |
| `reset --yes` | Return each preference to its default. |

```console
$ meshterm preferences show
PREFERENCE                   VALUE                                 DEFAULT                               DESCRIPTION
set_clock_on_connect         off                                   off                                   Set the device clock from this computer when it connects
trace_cooldown_s             5                                     5                                     Wait this long before transmitting again
flood_advert_cooldown_s      60                                    60                                    Extra wait before another mesh-wide advert
weekly_flood_advert          off                                   off                                   Flood an advert after a week without one
advert_quiet_s               30                                    30                                    Silence to wait for before the weekly advert
direct_message_soft_retries  0                                     0                                     Times to resend a message that gets no reply
tx_opt_min                   18                                    18                                    Weakest power it will try
tx_opt_max                   28                                    28                                    Strongest power it will try
tx_snr_tolerance_db          1                                     1                                     Signals this close are a tie, so less power wins
watch_silence_hours          12                                    12                                    Warn when a watched node goes quiet this long
watch_alerts_kept            200                                   200                                   How many past alerts to keep
map_view_fraction            0.5                                   0.5                                   Share of nodes to fit on screen; 1 shows them all
basemap_tilejson_url         https://tiles.openfreemap.org/planet  https://tiles.openfreemap.org/planet  Where map images come from (next launch)
history_days                 365                                   365                                   Older ones are deleted; 0 keeps everything
chat_history_limit           200                                   200                                   Old messages to load when you open a chat
courier_history_kept         100                                   100                                   Finished outbox messages to keep
cli_time_format              relative                              relative                              Whether the command line prints ages or timestamps
fast_render                  on                                    on                                    Faster screen updates; turn off if it looks wrong (next launch)
full_width                   auto                                  auto                                  Use the extra column some terminals hide
color_depth                  auto                                  auto                                  How many colours to send this terminal (next launch)
console_setup                auto                                  auto                                  Move to a better terminal when this console can't draw MeshTerm
console_font                 6x12                                  6x12                                  Bigger text, or more rows on the handheld's panel
log_level                    WARNING                               WARNING                               How much MeshTerm writes to its log file (next launch)
```

**`DESCRIPTION`** says what each preference does, because the name of a key does not give
its full meaning. Values print in the form that `preferences set` accepts as input. A
number has no unit suffix, and a boolean is `on` or `off`. If `VALUE` is different from
`DEFAULT`, this install has an override.

The overrides are in `preferences.toml` in [MeshTerm's state
directory](#where-meshterm-keeps-its-state). This file lists only what you changed. If you
delete a line, the default is in effect again.

**`--json`** is an array of `setting` objects that have `default` and `overridden`:

```console
$ meshterm preferences get trace_cooldown_s --json
{"key":"trace_cooldown_s","value":5.0,"type":"float","label":null,"redacted":false,"default":5.0,"overridden":false}
```

`overridden` gives the answer that the plain face makes you find by a comparison of two
columns. `DESCRIPTION` has no JSON counterpart. It is text for a person, and a consumer
that wanted it would render a settings screen. A boolean prints `on` or `off` on the plain
face and is `true` or `false` here. This is the one type for which a round trip through
`preferences set` needs a mapping. For each other type, `.value | tostring` goes back in
as it is.

`set` and `reset --yes` answer `{"changes":…,"applied":[…],"path":…}`. `path` is the
`preferences.toml` that MeshTerm wrote.

#### `meshterm about`, `about-author`, `discord`, `support`

The four written pages, printed as plain text. `about` says what MeshTerm is and under
which terms it ships. `about-author` says who wrote it. `discord` is the community
invitation. `support` says what keeps MeshTerm going.

```console
$ meshterm discord
Join the Discord
  Questions, ideas, bug reports, and mesh talk.

  • https://discord.gg/AZwe5Uvb3S
```

These four pages are the one place where the CLI wraps, at 72 cells. The menu draws a QR
code beside each link. The scripted face prints only the link.

**`--json`:**

```json
{"page":"discord","title":"Join Discord","version":"0.10.3","text":"Join the Discord\n  Questions, ideas, bug reports, and mesh talk.\n\n  • https://discord.gg/AZwe5Uvb3S","links":["https://discord.gg/AZwe5Uvb3S"]}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `page` | string | `"about"` \| `"about-author"` \| `"discord"` \| `"support"`. |
| `title` | string | The page's own title. |
| `version` | string | The version that the `{version}` placeholder of the page resolved to. |
| `text` | string | The whole page as the plain face prints it, raw, with the newlines. |
| `links` | array of strings | Each URL on the page, in the order of the document. |

`links` is the one thing on a page of prose that a program can use. It is also the one
thing for which the menu draws a QR code. `text` is the plain rendering of the page. It is
not the markdown changed into nested JSON. A structured markdown tree would be a second
rendering and not data, and no program would use it.

#### `meshterm diagnostics`

All the facts that a bug report starts with, in one block: which MeshTerm, on which host,
in which terminal, with which radio, and with how much stored history. It answers the
three or four questions that people otherwise ask one at a time in an issue thread. The
person who found the bug is usually the person who can least answer them, because
MeshTerm resolves half of these facts at boot and shows them on no screen.

This sample is the one exception to [About the examples](#about-the-examples). It is an
example report from a real radio over Bluetooth, because the report of the simulator says
little. The field names and the layout are what the command prints. The values are
examples.

```console
$ meshterm diagnostics
meshterm  0.9.0
install   frozen

os        Windows 11
os_build  10.0.26100
arch      AMD64
python    3.13.1

terminal       Windows Terminal
term           -
colorterm      truecolor
terminal_size  120x30
over_ssh       no
platform       regular
icons          yes
icons_source   windows-terminal
font           Cascadia Mono PL (windows-terminal)
powerline      full (font:windows-terminal)

connected       yes
transport       ble
port            -
device_role     companion
device_model    Heltec V3
firmware        v1.7.1 1c3f9a2
radio_freq_mhz  906.8750
radio_bw_khz    250.00
radio_sf        10
radio_cr        5
tx_power_dbm    20
error           -

config_dir   C:\Users\you\.meshterm
db_size_kb   41280
log_level    DEBUG
log_size_kb  96
runs_failed  3
first_heard  2026-01-14T19:42:10-05:00
last_heard   2026-09-21T11:03:00-04:00

TABLE                ROWS
app_state               4
discovered_paths     1287
messages             3316
neighbour_reports     214
observations       418203
path_candidates        96
runs                  912
schema_meta             1
trace_hops            233
traces                 47
tx_samples             68

PREFERENCE    VALUE
log_level     DEBUG
history_days  730
```

The seven groups are the seven sections of the page: build, host, terminal, radio,
storage, stored rows, and changed preferences. The command line shows them as records
that blank lines separate, because that face is a stream for `grep`. The page and the
saved file show them as markdown headings, because that face is a document to read.

**MeshTerm asks the radio, but it does not need the radio.** A report about a companion
that does not connect is the most useful report to file. Thus if MeshTerm cannot reach the
radio, the report has `connected no` and the radio's own words in `error`, and each other
fact still arrives.

**Only overridden preferences are in the list.** The defaults are in the source and are the
same for all users. The preferences that the reporter changed are the ones that can
explain a problem.

**The report describes the mesh only in totals.** It gives a count of rows for each table,
and the span of time that the observations cover. The counts come from the schema and not
from a list that a person wrote. Thus a table that someone adds later is in the report on
the day that it arrives.

**Nothing in the block is private.** It has no pairing PIN, no admin password, no channel
secret, no private key, no position, and no name or key of a contact. This is a property
of the feature and not a habit. The block is designed so that a person can paste it in
public without reading it. `tests/test_diagnostics.py` plants real secrets in each store
that holds one. It fails if one of them, or a field that is only *named* like one,
reaches either face.

With `--json`, the five blocks of facts merge into one flat object, and the two listings
keep a key of their own:

```json
{"meshterm":"0.9.0","install":"frozen","os":"Windows 11","…":"…","tables":[{"table":"app_state","rows":4},"…"],"preferences":[{"preference":"log_level","value":"DEBUG"},{"preference":"history_days","value":"730"}]}
```

**`--out PATH` writes the report to a file and does not print it.** The answer is then the
path. `config export-key --out` does the same. A caller who asked for a file wants to know
where the file is, and does not want the contents that went into the file.

```console
$ meshterm diagnostics --out meshterm-diagnostics.md
✓ diagnostics written — attach it to the report.
meshterm-diagnostics.md
```

**The file is markdown**, for each face that the run printed, because an issue tracker
renders markdown as it is written. The file needs no fence. Each value is a code span.
Thus the backslashes of a Windows path and the asterisks of a firmware error arrive as
they are, and not as markdown. Redirection still works, and `--out` does not replace it.
`--out` gives you the document, and redirection gives you the stream of records.

In the menu, this is the **Diagnostics** page under *This app*. It is one of the app's
**written pages**. It uses the same markdown renderer as the About pages, and it draws the
same document that the file holds. This helps a long block in the way that it needs most.
The `##` headings are landmarks. Each one stays at the top row while its own section
scrolls under it, and `^PgUp` and `^PgDn` step from section to section.

`s` saves. The footer hint names the key on a desktop. On the PicoCalc, the free **F3**
slot of the lane names it. The body of the page never names it, because the body belongs
to the report. The save writes `meshterm-diagnostics.md` into the config directory beside
the log, and it answers in a dialog that names the path. If the write fails, the same
dialog says so, in red, and the page stays readable.

#### `meshterm platform`

A smaller command than `diagnostics`. It shows which UI flavour a run resolves to, and
why. It does not ask the radio anything.

```console
$ meshterm platform
platform           regular
platform_source    default
flag               -
env                -
device_tree_model  -
icons              yes
icons_source       no-console
```

`icons` is a separate question from the flavour. It is the usual reason why a Windows
session looks plainer than the screenshots. With `--json`, the keys are the same, and
`icons` is a boolean:

```json
{"platform":"regular","platform_source":"default","flag":null,"env":null,"device_tree_model":null,"icons":true,"icons_source":"no-console"}
```

#### `meshterm specimen`

Print the specimen of the visual language: each mark, icon, colour scale, and fold on one
card.

**This is the one command that keeps its colour.** It builds its own themed console to do
this. This is deliberate, because the colour *is* the output. On the PicoCalc console, it
is the acceptance card for the font and the palette. With `--platform picocalc-lyra`, it
shows a preview of that flavour from a desktop. A monochrome specimen would test nothing.

The command also **refuses `--json`**, as a usage error (exit `2`). A colour card has no
data face, and a document of it would be false or without use.

#### `meshterm emulate`

Open a window that shows MeshTerm as the screen of a handheld draws it: `cardputer-zero`
or `picocalc-lyra`.

```console
$ meshterm emulate --fetch-fonts
$ meshterm emulate picocalc-lyra --mock --scale 2
```

It is not a strict emulator. MeshTerm runs on this machine as itself. The window copies
the limits of the handheld's display: the size of the display in pixels, its grid, its
font, and the colours that it can show. It also copies the keys that drive its F-key
lane. Each global option (`--mock`, `--port`, `--ble`, `--db`…) goes to the MeshTerm in
the window. [The emulator](../devices/README.md#the-emulator) describes the keys and the
font.

| Option | |
| --- | --- |
| `DEVICE` | `cardputer-zero` or `picocalc-lyra` |
| `--scale N` | the zoom of the window, in whole pixels (default `3`) |
| `--fetch-fonts` | download the Terminus font in which MeshTerm draws the screens, then stop. You need this one time |
| `--archive FILE` | install the font from a release archive that you downloaded in some other way |

The command needs an install with pip or pipx, because the one-file downloads do not
include Tk. In the same way as `specimen`, it refuses `--json` (exit `2`), because a
window is not a document. If the window cannot open, because there is no font yet or no
Tk, the command exits with `1` and prints the reason on stderr.

---

## Recipes

Use `--json` and `jq` for structured data. The plain face is for you to look at.

**The key of each repeater.**

```bash
meshterm contacts --json | jq -r '.[] | select(.node.type == "repeater") | .node.key'
```

**The name and the last-heard time, separated by a tab**, for a spreadsheet or `column -t`:

```bash
meshterm contacts --json | jq -r '.[] | [.node.name, .heard_at] | @tsv'
```

**Watch a node and send an alert if it goes quiet.** When `trace` returns `5`, the walk
ran and nothing came home.

```bash
#!/usr/bin/env bash
target=${1:?usage: watch NODE}
meshterm trace --target "$target" > /dev/null
status=$?          # capture it first: `if ! cmd` would have reset $? to 0
if [ "$status" -eq 5 ]; then
    echo "$target did not answer" | mail -s "mesh alert" me@example.com
fi
```

**The bottleneck hop of a walk.** This is the number that most discussions about a route
are about:

```bash
meshterm trace-path --path "a1,d4" --json \
  | jq -r '.edges | min_by(.snr_db) | "\(.from.hash) -> \(.to.hash)  \(.snr_db) dB"'
```

**A route as one line**, with our node, as the plain face draws it:

```bash
meshterm trace-path --path "a1,d4,a1" --json \
  | jq -r '.route | map(.name // .hash) | join(" -> ")'
```

**Capture a time window of traffic and make the totals yourself.** The summary block of
the plain face has no JSON counterpart. This is deliberate. The records that it comes from
are all there.

```bash
meshterm monitor --seconds 300 --json > capture.ndjson
jq -s -c 'group_by(.node.hash)[]
    | {hash: .[0].node.hash,
       name: (map(.node.name) | map(select(. != null)) | first),
       packets: length,
       best_snr_db: (map(.snr_db) | max)}' capture.ndjson
```

**One value into a variable.** `get` prints the bare value, because you named the key:

```bash
sf=$(meshterm config get radio_sf)
[ "$sf" -lt 9 ] && meshterm config set radio_sf 9
```

**A nightly archive of the config.** Keep the path that the command wrote:

```bash
out=$(meshterm config backup "$HOME/mesh/$(date +%F).toml") && echo "archived $out"
```

**Queue a message for a contact who is offline.** Then send it again later:

```bash
id=$(meshterm courier queue Alice "call me" --json | jq -r .id)
# ... later ...
meshterm courier send "$id"
```

**Sample a trace with a wait between runs.** By design, a trace transmits exactly one time
in each run. Set the wait between the runs yourself:

```bash
for i in $(seq 5); do
    meshterm trace --target Alice --path "3d,f2,3d"
    sleep "$(meshterm preferences get trace_cooldown_s)"
done
```

**Let MeshTerm advertise instead of cron.** A companion never advertises by itself, so
MeshTerm can do it for you. It advertises one time each week, and only when the air is
quiet:

```bash
meshterm preferences set weekly_flood_advert on   # starts the week; sends nothing now
meshterm preferences set advert_quiet_s 60         # wait for a minute of silence first
```

A running MeshTerm session then floods one advert from a device when the device has gone
a full week without one. A flood advert that you send by hand starts that device's week
again. MeshTerm sends the advert only after it has heard the mesh, and then heard nothing
for the quiet time plus a random 0 to 5 s.
**Do not put `config advert --flood` in a cron slot instead.** Each repeater in range
rebroadcasts a flood advert. A repeater that decides that you send bursts can blacklist
you.

**Quiet output for cron.** `--quiet` removes all console logging. Only the command's own
output and each error line remain. The `PATH` of cron is almost empty, so give the
absolute path:

```cron
0 * * * * /usr/local/bin/meshterm --quiet --profile yagi contacts --json > /var/log/mesh/contacts.json
```

---

## What has no command

Some features have no scripted face. This is the correct answer, and it is not a worse
version of the feature. Each of these features is a *live picture*. Its meaning is where
things are on a grid, how they move, and what colour they are. None of this stays when
MeshTerm changes the picture to records.

| Feature | Why there is no command, and what to use instead |
| --- | --- |
| **Map** | A drawing of braille cells in which colour shows which node is which. If MeshTerm removed the colour, as in the rest of the CLI, the map would be unreadable. If it kept the colour, you could not pipe the map to a useful place. `meshterm contacts` lists the same nodes, with the coordinates. |
| **Dashboard** | A live overview that MeshTerm paints again each second. `meshterm monitor` captures the same traffic as records. |
| **Live feed** | Each packet when it arrives, with the newest first, and a viewer behind each row. `meshterm monitor` is the scripted tail. |
| **Watchtower** | A background sentinel for starred nodes. It exists to *interrupt* a session. Build the same alarm from the exit status of `meshterm trace` (refer to [Recipes](#recipes)). |
| **Time machine** | Braille charts over a time window that you can switch. The history under them is in the database that `--db` points at. |
| **Mesh walk** | An evidence graph that you walk one node at a time. `meshterm records` and `meshterm trace` cover the walks that it is built from. |

Each other part of the menu has a command, in the list above, and each of these commands
accepts `--json`.
