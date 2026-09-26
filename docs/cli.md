# The MeshTerm command line

MeshTerm has two front ends over one codebase. The **menu** — what you get by running
`meshterm` with no arguments — is a full-screen session meant to be read by a person. The
**command line** is the other one, and this is its manual: what it prints, what it
returns, and every command it answers to.

It prints two ways, and the difference is who is reading.

- The **plain face** is for somebody at a prompt who typed a command to find something
  out. It is a Unix utility's output — one record per line, no colour, no frames — and it
  is allowed to be *comfortable*: names are bare, times read `5m` rather than
  `2026-09-08T04:26:00-04:00`, and a route is drawn with arrows.
- The **JSON face** (`--json`) is for a program, often on another machine and often later.
  It is the contract: typed values, absent spelled `null`, timestamps in UTC, keys that do
  not move. It works on **every** command.

The promise to be read by a program belongs to `--json`. The plain face makes no such
promise. If you are splitting output on whitespace to feed a script, stop: `--json` exists
so you do not have to, and the plain face may change between releases.

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

With no command, MeshTerm launches the interactive menu instead.

| Option | What it does |
| --- | --- |
| `--version` | Print `meshterm <version>` and exit `0`, before anything else is opened. |
| `-p`, `--profile NAME` | Use a named device profile from `config.toml`. |
| `--port PORT` | Serial port to connect to (`COM5`, `/dev/ttyACM0`), overriding the profile. |
| `--ble ADDRESS` | Bluetooth address of a companion; selects the Bluetooth transport. |
| `--ble-pin PIN` | Pairing PIN, if the Bluetooth companion asks for one. MeshTerm pairs with it itself on Windows and Linux; on macOS the system asks for the code in its own dialog, and this option has no effect. See [When a companion won't connect](connecting.md#bluetooth). |
| `--tcp HOST[:PORT]` | Network address of a TCP companion; selects the TCP transport. Default port 5000. |
| `--spi` | The LoRa radio on this machine's own SPI bus; selects the SPI transport. MeshTerm runs the node for the length of the command. With one radio attached, that's the one, profile or not; with none attached, the one SPI profile, else the uConsole AIO's wiring. With two, it refuses (exit `3`) and lists them — pick one with `-p`. See [Adding a radio on the SPI bus](configuration.md#adding-a-radio-on-the-spi-bus). Linux only. |
| `--mock` | Use the built-in simulator instead of real hardware. Nothing transmits, and the run records to `meshterm-mock.db` rather than your real history. |
| `--db PATH` | Use this SQLite database instead of `~/.meshterm/meshterm.db` (or `meshterm-mock.db`, under `--mock`). **It moves the database and nothing else** — see [Where MeshTerm keeps its state](#where-meshterm-keeps-its-state). |
| `--json` | Print the answer as JSON instead of aligned text. |
| `--absolute` | Print times as ISO-8601 instants rather than relative ages, for this run. |
| `-q`, `--quiet` | Suppress console logging entirely (the log file still records). |
| `--platform NAME` | Force the UI flavour (`regular`\|`picocalc`) instead of detecting it. |

**A global option may be typed anywhere** — before the command or after it. `meshterm
--json contacts` and `meshterm contacts --json` are the same run, because `-o json` goes
after the verb in every tool that has one and `--json` is the first thing anyone reaches
for. A bare `--` stops the lifting, the standard way to say the rest is data, and
`--help` is never lifted: `meshterm contacts --help` stays the *contacts* help.

Commands that need a radio open one, do their work, and close it. Commands that only read
stored history (`records`) or MeshTerm's own state (`preferences`, `platform`, `specimen`,
the written pages) need no device at all and work with nothing attached.

> **One transmission per invocation.** A trace transmits exactly once — repeaters
> penalise, and can blacklist, nodes that burst traffic. To sample more, run the command
> again with your own pacing between runs. `tx-optimize` is the one deliberate exception,
> and it paces itself.

---

## Where MeshTerm keeps its state

Everything MeshTerm remembers lives in one directory. On Linux and macOS that is
`~/.meshterm`; on Windows it is `%USERPROFILE%\.meshterm`. **`$MESHTERM_HOME` overrides
it**, and it is the only thing that moves the *whole* of it:

```bash
MESHTERM_HOME=/tmp/mesh-scratch meshterm contacts
```

The directory is read fresh on every run rather than cached, so two radios can be run out
of two directories from the same shell.

| File | What it holds |
| --- | --- |
| `meshterm.db` | The history: every packet overheard, every trace walked, every message. **The one file `--db` moves.** |
| `meshterm-mock.db` | The same, for the simulator: where a `--mock` run records when it doesn't name a database itself. Absent until you run one. |
| `config.toml` | Machine setup — device profiles, where the database lives. Yours to write; MeshTerm only reads it. |
| `preferences.toml` | Your overrides of MeshTerm's own behaviour. Lists only what you changed. |
| `courier.json` | The outbox — queued messages, waiting and finished. |
| `admin.json` | Remembered repeater-admin passwords. |
| `devices.json` | The remembered default companion, and every device proven to speak the protocol. |
| `contacts.json`, `channels.json`, `settings.json` | Per-device caches of what the radio last told us, so a screen opens without a round-trip. |
| `adverts.json` | The weekly flood advert's clock: when it was switched on, and when each device's week began. |
| `mutes.json`, `watchtower.json`, `remote.json` | Muted channels; watched nodes and their alerts; per-node remote-admin cache and CLI history. |
| `regions.json` | The regions known by name — and which repeaters carry each — plus every channel's send scope and each repeater's last answer to "which regions do you carry?". What a scoped packet's region is named against. |
| `radio/<spidev>/` | An SPI radio's node, one folder per device node (`radio/spidev1.0/`): its identity key, settings, channels, and contacts, and its log, `node.log`. What firmware would keep in flash. |
| `tilecache/` | Downloaded basemap tiles. |
| `meshterm.log` | The log file. |
| `.lock` | The single-instance lock. |

**`--db` alone does not isolate a run.** It moves the database, which is most of the
weight but none of the identity: the contact and channel caches, the outbox, the stored
admin passwords, and the remembered device all live beside it in the config directory and
would still be the ones you use every day. To run against a scratch state — a test, a
demo, a second radio — set `MESHTERM_HOME`, and set `--db` inside it if you want the
database somewhere else again. The same holds for the database `--mock` picks for itself:
it keeps the simulator's invented mesh out of your history, not out of the caches.

---

## About the examples

**Every sample below was captured from a real run against the built-in simulator**
(`--mock`), in a throwaway `MESHTERM_HOME`, except the `diagnostics` sample, which says so
where it appears. None of it is typed out by hand. The simulator
answers as a companion would — it has four contacts (`Alice`, `Yagi-Repeater`,
`Local-Repeater`, `Observer-Bot`), it adverts, it replies to traces — so the shapes are
real even where the numbers are invented.

Three things the simulator does that a radio does not, all visible in the samples:

- **Each invocation opens a fresh radio.** A setting written by one command, or a channel
  slot filled by one, is not there for the next — so `config show` always reports the
  defaults and `channels list` always answers `5`. On real hardware the radio remembers.
- **`PKTS` in `contacts` stays `-`.** The overheard tally is keyed by the node id passive
  monitoring stores (four bytes here) and the contact row is addressed at the device's
  path-hash width (one byte), so the two do not meet. Against real hardware at a matching
  width they do.
- **A device-routed `trace` invents its hop names.** `trace --target NAME` with no
  `--path` comes back through the simulator's router carrying placeholder hops; a
  *forced* path (`--path "a1,d4"`) resolves properly. The forced form is what the samples
  use, and what to build a golden file from.

> **`--mock` records into its own history, not yours — but it still writes the caches.**
> The simulator is a fake radio, not a fake MeshTerm: what it adverts is written down like
> anything else. The database it writes is `meshterm-mock.db`, so its invented nodes stay
> out of your mesh walk, dashboard, and map; naming a database with `--db` overrides that
> choice. The contact and channel caches, the outbox, and the remembered devices are still
> the ones you use every day, so a demo that should touch nothing at all gets a home of
> its own: `MESHTERM_HOME=... meshterm --mock …`.

---

## The plain face

Still a Unix utility's output: one record per line, nothing folds, stdout carries the
answer and nothing else. What has gone is every rule that existed *only* to make splitting
safe, because splitting is `--json`'s job now.

**No colour.** Not a hue, not a bold, not a dim — stdout carries no escape sequence at
all, whether or not it is a terminal. A colour that survives into a file is noise in it.

**No frames.** No borders, no boxes, no rules, no titles, no legends. The command you
typed is the title.

**Alignment is the delimiter, and names are bare.** A listing is an uppercase header line
and space-aligned records, the shape `ps` and `df` print. Numeric columns right-align.

```console
$ meshterm contacts
NAME            TYPE      HEARD  PKTS  HASH  LOCATION            KEY
Observer-Bot    node         7m     -  c3    45.48800,-73.58100  c3d4e5f600000000000000000000000000000000000000000000000000000000
Local-Repeater  repeater     7m     -  b2    45.47680,-73.59900  b2c3d4e500000000000000000000000000000000000000000000000000000000
Yagi-Repeater   repeater     7m     -  a1    45.50190,-73.56740  a1b2c3d400000000000000000000000000000000000000000000000000000000
Alice           node         7m     -  d4    -                   d4e5f6a700000000000000000000000000000000000000000000000000000000
```

Names are not quoted. Quoting would give a script a delimiter and give every person a
line full of punctuation. Names **are escaped**: a node broadcasts its own name and a
stranger fills in a message body, so neither may hold a raw control character or end the
record it sits in.

**A time is an age**, because "recently?" is the question a listing is opened to answer:
`now`, `5m`, `3h`, `never`. An absolute instant survives where the instant *is* the fact —
the device clock, an appointment set with `--at`, a live capture's own `TIME` column.

**`--absolute` turns every age back into an instant**, local ISO-8601 to the second:

```console
$ meshterm --absolute contacts
NAME            TYPE                           HEARD  PKTS  HASH  LOCATION            KEY
Observer-Bot    companion  2026-09-08T04:30:50-04:00     -  c3    45.48800,-73.58100  c3d4e5f600000000000000000000000000000000000000000000000000000000
Local-Repeater  repeater   2026-09-08T04:30:49-04:00     -  b2    45.47680,-73.59900  b2c3d4e500000000000000000000000000000000000000000000000000000000
Yagi-Repeater   repeater   2026-09-08T04:30:49-04:00     -  a1    45.50190,-73.56740  a1b2c3d400000000000000000000000000000000000000000000000000000000
Alice           companion  2026-09-08T04:30:48-04:00     -  d4    -                   d4e5f6a700000000000000000000000000000000000000000000000000000000
```

The flag is an **override of a preference**, not a switch beside one: `cli_time_format` is
`relative` or `absolute`, and `meshterm preferences set cli_time_format absolute` makes it
the default for every run. `--absolute` sets it for this run and never saves it. (How
MeshTerm behaves is a preference by this project's own rule; a flag that bypassed the
registry would be a behaviour nobody could find.)

**A route is drawn and a path is typed.** They are different things and they now look
different. Two lines out of a trace's facts block:

```
path        a1,d4
route       MockCompanion (00) → Yagi-Repeater (a1) → Alice (d4) → MockCompanion (00)
```

A **path** is a *spec* — the hop list you compose and force, and the one line on either
face that round-trips, so it stays comma-separated hex and nothing creeps into it. Paste
it straight back into `--path`. A **route** is the concrete sequence a walk actually took;
it is read, not typed, so it reads like what it is. A hop is `Name (hash)`, or whichever
half is known, never an empty `()`.

**Our own node is a hop like any other**, named and hashed — never the menu's star. The
star says "you already know who this is", which is true of the person watching and false
of whoever opens the file afterwards. The hash is there because the CLI has no colour: on
screen a route's hops are told apart by their key-derived hues, and in plain text the hash
is what carries that identity, and what joins a route line to the per-hop table under it.

**Key/value blocks** are a key, the gutter, and the rest of the line as its value — the
shape `sysctl -a` prints. No header; the value is whatever follows the key.

```console
$ meshterm info
name              MockCompanion
public_key        0000000000000000000000000000000000000000000000000000000000000000
role              companion
battery_v         4.10
uptime_s          93784  (1d 2h)
noise_floor_dbm   -110
```

A reading whose raw number is the fact but whose *meaning* is a duration carries a gloss
in parentheses. `93784` is what the key promises and what a caller wants; `(1d 2h)` is
what a person actually asked. Neither has to be parsed out of the other.

A `get` prints the bare value alone, ready for `$(...)`, because the caller named the key:

```console
$ meshterm config get tx_power
20
```

**`-` is the one token for absent** — unknown, or not applicable. You learn it once; there
is no em dash, no `n/a`, no empty cell. **`never` is different**: it is a *value*, the fact
that a node has not been heard yet. And a node type stays the word `repeater` — a
monochrome glyph would need a legend, and the CLI has no legends.

**A live stream pins its lanes.** `monitor` and `chat listen` cannot measure a column they
have not seen yet, so their headings are fixed in advance. A value wider than its lane
overruns and pushes the row right; nothing is elided, and the one unbounded field goes
last so it can push nothing.

**No wrapping, with one exception.** A record is a line; wrapping would put half a
record's fields under the wrong headings, so nothing folds and a long line runs off the
right. The exception is the four written pages (`about`, `about-author`, `discord`,
`support`), which wrap at 72 cells. The no-wrap rule exists to protect a record's
fields; a paragraph has no fields, and unwrapped it is a 600-cell line no terminal can
read.

**stdout is the answer; everything else is stderr** — errors (`meshterm: what went
wrong`), progress bars, log records, acknowledgements, and closing messages:

```console
$ meshterm config set radio_sf 9
$ meshterm config set radio_sf 9 2>&1
✓ radio_sf = 9
✓ applied 1 change
```

An **acknowledgement** ("✓ device clock set") and a command's closing **message** ("0
records stored") are reassurance a person needs and a script does not. Sending them to
stderr means `meshterm contacts > contacts.txt` puts contacts in the file and nothing
else, while the person at the prompt still sees their tick and their count.

---

## The JSON face

`--json` prints **the answer itself**, and the report a caller needs is still `$?`.

**No envelope.** An array for a listing, an object for a set of facts. So this reads like
what it looks like:

```console
$ meshterm contacts --json | jq -r '.[] | select(.node.type == "repeater") | .node.key'
b2c3d4e500000000000000000000000000000000000000000000000000000000
a1b2c3d400000000000000000000000000000000000000000000000000000000
```

A command whose plain face prints two blocks (`trace`, `tx-optimize`) is one object, each
listing under its own key (`edges`, `levels`) and the facts merged at the top level.

**One compact line, newline-terminated.** UTF-8 with no ASCII escaping, so a name in
Cyrillic stays a name. Keys come in the order the report declares them, never sorted — a
document reads down the page in the order its plain sibling does. `jq .` re-indents at no
cost; nothing can un-stream a document pretty-printed across 400 lines.

**A stream is one document per record, all the same shape.** `monitor` and `chat listen`
emit newline-delimited JSON as it arrives, flushed. There is no `begin` or `end` line and
no discriminator field: every reader would pay for one and only a stream would use it. A
capture survives truncation — you lose the last line, not the file — and a run ended by
Ctrl-C, which is how `--seconds 0` is *designed* to end, still leaves everything it heard.

**Absent is `null`, never an omitted key.** Every field a command declares appears in
every document, carrying `null` when the value is absent, unknown, or does not apply.
`null` is the counterpart of the plain face's `-` and, like it, is learned once. An empty
collection is `[]` or `{}`. An empty string is `""` and is a *value*, distinct from
`null`. A whole object is `null` where its parts are meaningless apart: `"position":
null`, not `{"lat": null, "lon": null}`. And `unknown` becomes `null` — a consumer already
has one spelling for "nothing here".

**Values are typed.** `9`, not `"9"`. `false`, not `"false"`. `yes`/`no` in plain
(`acked`, `applied`, `success`, `active`) is `true`/`false` here. A message body is raw
and unescaped, newlines and all, because a JSON string is not a line.

**Timestamps are UTC, RFC 3339, `Z`, to the second** — `"2026-09-08T08:30:50Z"`, twenty
characters, so string comparison is time comparison. A field holding one is named
`<something>_at`: `heard_at`, `observed_at`, `created_at`, `scheduled_at`. **`--absolute`
does not touch this.** A local offset is a fact about the machine that ran the command,
not about the event, and two hosts asking the same radio the same question must produce
the same document.

**A numeric field carries its unit in its key** — `snr_db`, `rssi_dbm`, `uptime_s`,
`rtt_ms`, `battery_v` — never in its value, and a number carries its own sign (`5.1` and
`-1.5`, never the plain column's `+5.1`). **A key is never truncated**: a truncated key
cannot go back into `--to` or `--path`, so a short id is a `hash` and says so.

**`--json` changes the rendering, never the report.** Same exit status, same records. An
empty result prints its empty document and **still exits 5**:

```console
$ meshterm channels list --json
[]
$ echo $?
5
```

**"Prints nothing" is never a legitimate JSON answer.** A command whose plain face is
silent still says what it did:

```console
$ meshterm channels join 3 brigade 4ed883aaded6433c6606c9930a65044c
$ meshterm channels join 3 brigade 4ed883aaded6433c6606c9930a65044c --json
{"channel":{"slot":3,"name":"brigade","public":false,"hash":"5a"},"secret":"4ed883aaded6433c6606c9930a65044c","url":"meshcore://channel/add?name=brigade&secret=4ed883aaded6433c6606c9930a65044c"}
```

**A failure prints nothing on stdout.** The sentence stays on stderr and the
classification stays in `$?`. There is no error document to tell apart from a result, and
`--json 2>/dev/null` still behaves — see [When it goes wrong](#when-it-goes-wrong).

**A command with no data face refuses, loudly.** `meshterm specimen --json` is a usage
error (exit 2): its output *is* the colour, and a document of it would be a lie or an
empty gesture. So is `--json` with no subcommand — there is no document an interactive
session can emit.

### The shapes that repeat

Defined once here; the per-command tables below name them rather than restating them.

**`node`** — a mesh participant's identity, and nothing else. All five keys always
present.

```json
{"name":"Yagi-Repeater","key":"a1b2c3d400000000000000000000000000000000000000000000000000000000","hash":"a1","type":"repeater","self":false}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string \| null | The name the node advertises. Never quoted, never truncated. |
| `key` | string \| null | The full public key, lowercase hex, 64 characters. `null` where only a hash was ever heard. |
| `hash` | string \| null | The short derived id, at the width the surrounding surface addressed the node by. Never `key` cut short. |
| `type` | string \| null | `"companion"` \| `"repeater"` \| `"room server"` \| `"sensor"` \| `null`. |
| `self` | boolean | `true` for our own node. What the menu's star says and the plain face deliberately does not. |

Reception facts — when heard, how strong, where — belong to the **row**, not to the node:
they differ per surface, and a node object carrying them would mean something different in
each one. So a row embeds `node` under its own key rather than flattening it, and
`jq '.[].node.key'` reads the same on contacts, courier and monitor.

> **Identity is thin where it comes from history.** A node MeshTerm only ever overheard
> has `key: null` and `type: null`, and `name` is whatever the resolver could find. To
> correlate across commands, join on `hash` **plus** the width it was addressed at, not on
> `key`.

**`position`** — `{"lat":45.5019,"lon":-73.5674}`, decimal degrees as stored (unrounded;
the plain face's five decimals are a column-width concession). The whole object is `null`
where the node has shared no location.

**`channel`** — `{"slot":0,"name":"Public","public":true,"hash":"11"}`. `slot` is what
every `channels` subcommand's `INDEX` takes; `public` is the plain face's `TYPE` column as
a boolean. **The secret is never in a `channel` object** — it appears only where the
caller asked for it, under its own key.

**`setting`** — one device setting or preference, the same shape `show` and `get` both
use.

```json
{"key":"radio_sf","value":8,"type":"int","label":null,"redacted":false}
```

`type` is `"int"` \| `"float"` \| `"bool"` \| `"str"` \| `"enum"`. `value` is the typed
current value — an enum is its **number**, so `.value | tostring` is what `set` takes
back. `label` is the reader's word for an enum's number, else `null`. `redacted` is `true`
where the value is deliberately withheld, and a redacted row's `value` is `null` — a mask
string is not a value. Preference rows add `default` and `overridden`.

**`route` and `edges`** — a `route` is an array of `node` objects in propagation order,
our own node a hop like any other; `null` when nothing came home, never `[]`. An `edge` is
one directed link, `{"index":0,"from":{node},"to":{node},"snr_db":6.0}`, with the SNR
measured arriving at `to`. Where the plain face names an edge's ends by hash so the reader
can join the hop table to the route line above it, JSON embeds the whole node at both
ends: the join is structural here, and each edge should be readable on its own.

---

## Exit status

The return value is half the report, on both faces.

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Failure with no more specific code — an unreadable file, an unknown setting or preference key (`config set`, `preferences set`), an unhandled fault. |
| `2` | Usage error: an unknown flag, a missing argument, a value the parser rejected, a confirmation not given. |
| `3` | No companion device could be selected: none attached, the named one could not be opened, or the choice was ambiguous. **Nothing was transmitted.** |
| `4` | A device was reached but the operation failed — a command error, a timeout, a link lost mid-run. Retrying is reasonable. |
| `5` | The command completed and there was **nothing to report**: an empty list, a target that never answered, a conversation with no messages. This is not an error. |

The table is printed under `meshterm --help` as well.

Two boundaries need stating. **Nothing transmitted is not a device failure**: a value
the tool refuses before it opens the radio is a usage error, so a missing `--yes`, a bad `--sort` or `--category`, an unknown
`--profile`, an outbox id that does not exist, and `tx-optimize` with no password
available are all `2`. And **a named connection that will not open is `3`, not `4`**: a
`--port` that is not there means no device was selected, and nothing went out.

`5` is the one worth building on. `grep` conflates "found nothing" with "worked", and a
caller that has to count output lines to tell an empty mesh from a full one is parsing
when it could be branching:

```bash
if meshterm contacts > contacts.txt; then
    echo "$(($(wc -l < contacts.txt) - 1)) contacts"      # minus the header
elif [ $? -eq 5 ]; then
    echo "no contacts yet"
else
    echo "could not reach the radio" >&2
fi
```

`3` and `4` split the failures a retry might fix from the ones it never will:

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

A failure is **raised, never returned**: nothing lands on stdout, one sentence lands on
stderr, and the classification lands in `$?`. That holds on both faces — `--json` does not
wrap a failure in a document, because a consumer that can see `$?` does not need one and a
consumer that cannot would have to tell an error document from a result.

**No radio.** Nothing was transmitted; the exit is `3` whichever way the device was named.

```console
$ meshterm --port NOSUCHPORT99 info
meshterm: could not open serial port NOSUCHPORT99: NOSUCHPORT99 isn't there — the device was unplugged, or came back under another name.
$ echo $?
3
```

The sentence names the cause and the remedy: a port that is missing, in use by another
program, or not yours to open, and for Bluetooth a link that never opened, a pairing that is
out of date, or a PIN that was wrong. [When a companion won't connect](connecting.md) lists
every one of them.

With nothing named and more than one candidate attached, the same `3` arrives as a list
and the way out of it:

```console
$ meshterm info
meshterm: Multiple companion devices detected and no default to fall back on.
  • COM12 (serial) — USB Serial Device (COM12) [likely LoRa]
  • COM16 (serial) — USB Serial Device (COM16) [likely LoRa]
  • COM26 (serial) — USB Serial Device (COM26) [likely LoRa]
  • COM1 (serial) — Communications Port (COM1)
Choose one with --port <PORT> or --ble <ADDRESS> (or run 'meshterm devices' to inspect them). The chosen device is remembered as the default after it connects.
```

**A value the parser rejects** is `2`, and it names the choices rather than making you
find them:

```console
$ meshterm contacts --sort bogus
Usage: meshterm contacts [OPTIONS]
Try 'meshterm contacts --help' for help.

Error: Invalid value: --sort must be one of: name, heard, packets (got 'bogus')
```

**A confirmation not given** is the same class, because nothing has happened yet:

```console
$ meshterm config reboot
Usage: meshterm config reboot [OPTIONS]
Try 'meshterm config reboot --help' for help.

Error: Invalid value: reboot restarts the device. Re-run with --yes to confirm.
$ echo $?
2
```

Destructive operations gate behind `--yes` rather than a prompt, so nothing surprising
happens in a script.

**A refusal** is also `2` — the caller asked for something the command does not have:

```console
$ meshterm specimen --json
Usage: meshterm specimen [OPTIONS]
Try 'meshterm specimen --help' for help.

Error: Invalid value: specimen has no machine-readable output — its output is the colour
```

**The radio answered and the operation failed** is `4`, and it says what to do next:

```console
$ meshterm tx-optimize --path "Yagi-Repeater,Alice" --password wrong
logging in to Yagi-Repeater …
meshterm: admin login to 'Yagi-Repeater' failed (wrong password?). The saved password was cleared; re-run to enter a new one.
$ echo $?
4
```

**Nothing to report** is `5` and is not a failure at all. stdout is empty on the plain
face and carries the empty document under `--json`; anything MeshTerm wants to say about
it — `0 records stored` — goes to stderr like every other acknowledgement.

```console
$ meshterm records
$ echo $?
5
$ meshterm records --json
[]
```

Under `--json`, none of this changes: same statuses, same stderr, and stdout carries the
document on `0` and `5` and nothing at all on `1` through `4`.

```console
$ meshterm contacts --sort bogus --json
Usage: meshterm contacts [OPTIONS]
Try 'meshterm contacts --help' for help.

Error: Invalid value: --sort must be one of: name, heard, packets (got 'bogus')
$ echo $?
2
```

An **unhandled fault** exits `1` with its traceback in the log file. Everything above is
an expected failure, reported once.

---

## Command reference

Each entry gives what the command does, its options, what the plain face prints, and what
`--json` returns. Where a JSON field is one of the [shapes that
repeat](#the-shapes-that-repeat), the table names the shape rather than restating it.

### Finding a device

#### `meshterm devices`

List every attached serial device, every radio on this machine's SPI bus, and every
in-range Bluetooth companion. Opens no radio — it only enumerates what is attached or
advertising. An SPI radio is listed when its `/dev/spidev*` node exists, exactly as the
device screen lists it: one per SPI profile, plus the uConsole AIO's `/dev/spidev1.0` when
no profile covers it. Under `--mock` it enumerates nothing at
all: the simulator is the one device, listed with `--mock` as its target, and neither the
serial ports nor the Bluetooth radio are touched — a session that promised no real
hardware keeps the promise here too.

| Option | What it does |
| --- | --- |
| `--ble` / `--no-ble` | Include a Bluetooth LE scan. On by default; skipping it saves a few seconds. Moot under `--mock`. |

```console
$ meshterm devices --no-ble
TARGET  TRANSPORT  NAME                        HARDWARE               MESHCORE  SERIAL            ACTIVE
COM12   serial     USB Serial Device (COM12)   Adafruit               maybe     D030D2CCD00F08EB  no
COM16   serial     USB Serial Device (COM16)   Adafruit               maybe     D8D7D4BB5E4546E0  no
COM26   serial     USB Serial Device (COM26)   Espressif              maybe     F85B1BA5EAD0      no
COM1    serial     Communications Port (COM1)  (Standard port types)  no        -                 no
```

`TARGET` is what `--port` and `--ble` take, verbatim; an SPI radio's is its device node,
reached with `--spi` or its profile's `-p`. `MESHCORE` is three-valued and stays
that way: `yes` only once a connection has proved the device speaks the protocol, `maybe`
for a USB vendor ID that suggests a LoRa board or a bridge chip, `no` for anything else —
a vendor ID is a hint, and the column would be lying if it rounded one up.

**`--json`** is an array of targets in discovery order, carrying five facts the plain
table has no room for.

```json
{"target":"COM12","transport":"serial","port":"COM12","address":null,"label":"USB Serial Device (COM12)","hardware":"Adafruit","meshcore":"maybe","confidence":"board","serial_number":"D030D2CCD00F08EB","stable_id":"sn:D030D2CCD00F08EB","confirmed":false,"remembered":false,"active":false}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `target` | string | What `--port` / `--ble` take, verbatim. |
| `transport` | string | `"serial"` \| `"ble"` \| `"tcp"`, or `"mock"` under `--mock`. |
| `port` | string \| null | The serial port, for a serial device. `""` on the `--mock` row. |
| `address` | string \| null | The Bluetooth address, for a Bluetooth device. |
| `label` | string | The OS's name for the device, or the confirmed node name where we have one. |
| `hardware` | string \| null | The confirmed hardware model, else the USB vendor label. `""` on the `--mock` row. |
| `meshcore` | string | `"yes"` \| `"maybe"` \| `"no"`. Not a boolean — rounding a hint up to `true` would be a lie. |
| `confidence` | string | `"board"` \| `"bridge"` \| `"unknown"` — what a `"maybe"` was derived from. |
| `serial_number` | string \| null | |
| `stable_id` | string | The identity the device store keys on (`"sn:D030…"`, `"port:COM1"`). |
| `confirmed` | boolean | Whether this device is stored as a proven companion. |
| `remembered` | boolean | Whether it is the remembered default. |
| `active` | boolean | Whether this invocation is (or would be) using it. |

Under `--mock` there is no scan. The list is one row for the simulator: `target`
`"--mock"`, `transport` `"mock"`, `stable_id` `"mock:simulator"`.

Returns `5` when nothing is found.

---

### This node

#### `meshterm info`

The connected companion's live status: which radio this is, and how it is doing.

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

The unit is in the key, so nothing has to be pulled back out of prose. A reading whose
number is a **duration** carries a gloss beside it: `93784  (1d 2h)`. The number is what
the key promises and what a caller wants; the parenthesis is what a person actually asked.

`clock_at` is the *device's* clock and `clock_drift_s` is device minus host, signed. On the
plain face `clock_at` is one of the instants that survives a relative-age default, because
a clock reading whose whole point is what time the device thinks it is would say nothing
as `now`.

A reading the firmware does not answer for is **absent** rather than `-`: a missing key
says "this device does not report it", where a dash would claim it reported nothing.

**`--json`** is one object with the same keys, minus the glosses, and with every key
always present:

```json
{"name":"MockCompanion","public_key":"0000000000000000000000000000000000000000000000000000000000000000","role":"companion","model":"MeshCore Simulator","firmware":"mock mock","battery_v":4.1,"storage_used_kb":128,"storage_total_kb":1024,"clock_at":"2026-09-08T08:24:42Z","clock_drift_s":-125,"uptime_s":93784,"noise_floor_dbm":-110,"last_rssi_dbm":-62,"last_snr_db":9.5,"tx_air_s":42,"rx_air_s":360,"packets_sent":210,"packets_received":1234,"receive_errors":3}
```

`role` is `"companion"` \| `"repeater"` \| `"room server"` \| `"sensor"` \| `null`, or
`"type N"` for an advert type MeshTerm does not know. This is the one place the two faces
disagree about absence on purpose: plain omits a key the firmware never answered for, JSON
writes `null`, because a variable key set costs every consumer a lookup guard on every
field.

`info` does not embed a `node` object — it is our own node in far more detail than the
shape carries, and `name` / `public_key` here are its whole identity. What the radio is
*set to* is `config show`'s answer, not this one. Never returns `5`.

#### `meshterm config`

View and change every device setting. With no subcommand it runs `show`.

| Subcommand | What it does |
| --- | --- |
| `show` | Print every setting as `key value`. |
| `get KEY` | Print one setting's value, bare. |
| `set KEY VALUE` | Change one setting. |
| `backup PATH` | Write every setting to a TOML file. |
| `restore PATH [--dry-run]` | Apply settings from a TOML backup. `--dry-run` prints the plan and changes nothing. |
| `custom KEY VALUE` | Set an experimental custom variable. |
| `channel INDEX NAME [--secret HEX]` | Configure a channel slot (see also the `channels` group). |
| `advert [--flood]` | Broadcast an advertisement. Zero-hop unless `--flood`. |
| `share` | Print this node's contact card as a `meshcore://` URI. |
| `sync-clock` | Set the device clock from this computer. |
| `export-key [--out PATH]` | Export the private key. **Sensitive.** |
| `import-key KEY_HEX --yes` | Import a private key, overwriting this node's identity. |
| `reboot --yes` | Reboot the device. |
| `factory-reset --yes` | Erase all data and reset to defaults. |

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

`show` names each setting by the key `get` and `set` take, and prints a value they will
take back — an enum's **number**, not the reader's label; `""` for an empty string; `-`
for something the firmware never reported. So a line read out of `show` can be typed
straight back in, and the write says what it replaced:

```console
$ meshterm config get radio_sf
8
$ meshterm config set radio_sf 9 --json
{"changes":1,"applied":[{"key":"radio_sf","previous":8,"value":9}]}
```

(Read back with `config get` against real hardware and it answers `9`. The simulator these
examples run against opens a fresh radio per invocation, so a setting written by one
command is not there for the next.)

The pairing PIN stays masked here, because this is the whole-device dump — the thing that
gets redirected into a file and pasted into a bug report. Name it deliberately with
`meshterm config get device_pin`, which is the one place it is not withheld.

Custom variables are namespaced `custom.*` in the output, since they have no spec and
`config set` will not take them (use `config custom`).

**`--json`.** `show` is an array of `setting` objects in the same order; `get` is one.

```console
$ meshterm config get tx_power --json
{"key":"tx_power","value":20,"type":"int","label":null,"redacted":false}
```

Three shapes are kept apart: `flood_scope` is `""` (an empty *value*), `device_pin` is
`null` with `"redacted":true` (withheld on purpose — a mask string is not a value), and a
setting the firmware never reported is `null` with `"redacted":false`.

Every write emits what it did, where the plain face is silent and only the stderr
acknowledgement says so:

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

`previous` is `null` where the snapshot held no prior value; `changes` is `0` and
`applied` is `[]` where the value already was what was asked for, and the exit stays `0`
because the command did what it was asked. `backup`'s `path` is the file written, as you
named it (the plain face prints it bare, so a caller can keep it). `export-key` carries a private
key, exactly as the plain face does — that is what the command was asked for, and no extra
gate is introduced here. `factory-reset --yes` answers `{"factory_reset":true}`.

The QR code the menu draws beside `share` has no JSON face and never will: it is a second
rendering of `url`.

---

### The mesh around you

#### `meshterm contacts`

The contacts your device knows.

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

`TYPE` is the node's advertised role in words — `companion`, `repeater`, `room server`,
`sensor`, or `unknown`. `HEARD` is a relative age, because "recently?" is what this listing
is opened to answer; `--absolute` turns it into an instant. `PKTS` is how many packets passive
monitoring has overheard.

**`HASH` is the token `--path` and `--to` take**, so it never has to be sliced out of `KEY`
by hand, where getting the width wrong would address a different node. It is exactly the
device's path-hash width. **`LOCATION`** is a fact the model always carried and no column
had room for. `KEY` stays full and last, so it runs off the right harmlessly and is never
elided: a truncated key is not something a caller can hand back.

This lists *contacts only*. Your own node is not a contact — `meshterm info` reports it, in
far more detail than a row could hold.

**`--json`** is an array of rows, each embedding the `node` shape.

```json
{"node":{"name":"Observer-Bot","key":"c3d4e5f600000000000000000000000000000000000000000000000000000000","hash":"c3","type":"companion","self":false},"heard_at":"2026-09-08T08:30:50Z","packets":null,"position":{"lat":45.488,"lon":-73.581}}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `node` | object | The `node` shape. `hash` is at the device's path-hash width. |
| `heard_at` | timestamp \| null | When the contact was last heard. `null` is the plain face's `never`. |
| `packets` | integer \| null | Packets passive monitoring has overheard. `null` where none were, which is distinct from `0`. |
| `position` | object \| null | The `position` shape. |

Returns `5` and `[]` when the device knows no contacts yet.

#### `meshterm monitor`

Capture overheard packets in the foreground for a bounded window, then summarise it.
Transmits nothing; it only listens. Recording to history is always on anyway — this adds
the live view.

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

Two blocks, one straight after the other: the summary starts at its own `NODE` header
line, with no blank line before it. The sample shows only the first four stream records.
The **stream** is a header and then a record per packet as it arrives; its lanes are
pinned in advance, because a capture cannot measure a column it has not seen yet, and `TIME` is an absolute instant rather than an age — on a
live tail every row would otherwise read `now`. The **summary** is the aggregate the
stream cannot give: a count, a median, a best per node, most recently heard first.

**`--json` is the stream and only the stream.** One document per packet, every line the
same shape, and **no summary document at the end**:

```json
{"observed_at":"2026-09-08T08:30:40Z","node":{"name":"Yagi-Repeater","key":"a1b2c3d400000000000000000000000000000000000000000000000000000000","hash":"a1b2c3d4","type":"repeater","self":false},"kind":"advert","snr_db":7.0,"rssi_dbm":-101.3,"position":{"lat":45.5019,"lon":-73.5674},"scope":null,"path":null}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `observed_at` | timestamp | When the packet arrived. |
| `node` | object | The `node` shape. `hash` is the stored node id, four bytes wide. |
| `kind` | string | The packet class — `"advert"`, `"telemetry"`, `"packet"`, `"message"`, `"ack"`. |
| `snr_db` | number \| null | |
| `rssi_dbm` | number \| null | |
| `position` | object \| null | The `position` shape. |
| `scope` | object \| null | The region a flood was sent into: `{"state", "region", "code"}`. `state` is `"scoped"` (`region` names it), `"unknown"` (scoped, but no region known here reproduces `code`) or `"unscoped"` (a plain flood). `code` is the frame's transport code as lowercase hex, kept even when unresolved so two packets can be seen to share a region. `null` for a direct packet, or a class whose route was never reported — a repeater never region-filters those. The plain `SCOPE` lane prints the region, `unknown`, `unscoped` or `-`. |
| `path` | array of strings \| null | The relay chain the packet arrived by, one lowercase-hex hash per hop, in propagation order. `[]` is a direct reception; `null` means the packet class carries no path at all. This is the one place `path` is an array — it is a *received* relay chain rather than a composed spec. |

**This is the one place a whole block appears on one face and not the other, and it is
deliberate.** The per-node summary is a convenience for a person watching a capture end.
Every figure in it is computable from the records that scrolled past above it, and emitting
it as a second *shape* of line in the middle of an otherwise homogeneous stream would cost
every consumer a discriminator it would never otherwise need. So the aggregate stays on the
plain face, and JSON hands you what it was derived from:

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
Returns `5` when the window heard nothing.

#### `meshterm records`

The record-setting walks every trace has been scored against. Reads the database; needs no
device and never transmits.

| Option | What it does |
| --- | --- |
| `-c`, `--category ID` | One discipline: `long_haul`, `far_point`, `long_leg`, `grand_tour`, `clean_trail`, `thin_thread`, `big_loop`. |
| `-w`, `--width N` | Only records set at this per-hop hash width (`1`, `2`, or `4`). |

The listing is `CATEGORY WIDTH SCORE UNIT RECORDED VERSION ROUTE`. `SCORE` is a bare
number and `UNIT` is its own column, so the scores in a column are comparable; a score
prefixed `>=` is a **lower bound**, meaning some hop on that walk has no known position and
the distance is a floor rather than a measurement. Records are kept per hash width, because
the width bounds both a walk's maximum length and its collision odds — boards at different
widths are measuring different games.

```console
$ meshterm records
$ echo $?
5
```

Records are discovered by the menu's walking, so a database that has only ever been driven
from the command line has none — which is what the simulator these examples run against
prints, honestly.

**`--json`** is an array carrying two facts the columns cannot hold.

| Field | Type | Meaning |
| --- | --- | --- |
| `category` | string | The discipline id, as `--category` takes it. |
| `hash_bytes` | integer | The per-hop hash width the walk was transmitted at — the plain `WIDTH`, under the contract's word for the concept. |
| `score` | number | The bare score, unqualified. |
| `unit` | string | `"km"` \| `"nodes"` \| `"dB"` \| `"km²"`. |
| `lower_bound` | boolean | **JSON only.** `true` where the score is a floor — the plain face's `>=` prefix, extracted, so a consumer comparing scores need not string-match a qualifier off the front of its own data. |
| `recorded_at` | timestamp | |
| `version` | string | The MeshTerm version that discovered it. |
| `path` | string \| null | **JSON only.** The transmitted spec, comma-separated hex — what a caller re-walks with. It has no column because a route line already runs off the right. `null` where the record stored no spec. |
| `route` | array | The `route` shape, hop-aligned with `path`. |

Returns `5` and `[]` when no records match.

---

### Messaging

#### `meshterm chat`

`meshterm chat` on its own is not interactive — it prints this group's help and exits `2`.
The live transcript is a menu screen; from the command line, pick a subcommand.

| Subcommand | What it does |
| --- | --- |
| `send TEXT --to NAME` | Send a direct message. |
| `send TEXT --channel N [--scope REGION]` | Broadcast on a channel slot, under the channel's scope — or under `REGION` for this one message, `*` for unscoped. |
| `history [--to NAME \| --channel N] [--limit N]` | Print a conversation's stored transcript. Default limit 50. |
| `list` | Every channel and contact with its unread count and last message. |
| `listen [-s SECONDS] [--debug]` | Tail inbound messages live. `-s 0` (the default) runs until Ctrl-C. |

Exactly one of `--to` and `--channel` is required on `send` and `history`.

```console
$ meshterm chat send "on my way" --to Alice
acked  yes
```

A direct message prints its `acked` state, which is the one thing the send does not
already tell you — the radio accepted it either way, and whether the peer answered is a
separate fact. A **channel** broadcast prints nothing: there is no acknowledgement on a
channel, so the exit status is the whole answer.

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

`DIR` is `in` or `out`; `PEER` is always the *other* party. `TEXT` is last and is the rest
of the line — a message body is the one field that can hold absolutely anything, so
nothing may follow it.

```console
$ meshterm chat list
CONVERSATION    KIND     UNREAD   LAST  LAST_TEXT
Public          channel       0  never  -
Yagi-Repeater   direct        0  never  -
Local-Repeater  direct        0  never  -
Observer-Bot    direct        0  never  -
Alice           direct        0  never  -
```

```console
$ meshterm chat listen -s 3
TIME                       PEER              SNR_DB  TEXT
2026-09-08T04:30:44-04:00  b2c3d4e5            +4.9  hello from Local-Repeater #1
2026-09-08T04:30:44-04:00  c3d4e5f6            -0.6  hello from Observer-Bot #2
2026-09-08T04:30:45-04:00  d4e5f6a7            +3.9  hello from Alice #3
```

`listen` pins its lanes and stamps each row with an instant, the same shape `monitor`'s
stream takes and for the same reason. `--debug` routes the message-pull logging to stderr,
for working out whether messages are being pulled from the companion at all.

**`--json`.** `send` reports what it sent and to whom:

```console
$ meshterm chat send "see you there" --to Alice --json
{"kind":"direct","node":{"name":"Alice","key":"d4e5f6a700000000000000000000000000000000000000000000000000000000","hash":"d4e5f6a7","type":"companion","self":false},"channel":null,"sent":true,"acked":true}

$ meshterm chat send "net in 5" --channel 0 --json
{"kind":"channel","node":null,"channel":{"slot":0,"name":"#0","public":true,"hash":null},"sent":true,"acked":null}
```

`kind` is `"direct"` \| `"channel"` and says which of `node` / `channel` is populated.
A channel send also carries `scope`, in the same shape `monitor` gives a received packet's:
`{"state":"scoped","region":"harbour","code":null}` for a region, `"unscoped"` for a plain
flood, and `null` where it isn't known (a direct message, or a channel with no scope of its
own whose device default couldn't be read). `code` is `null` on a send: the code is a hash
of the packet, and the radio builds the packet.

`--scope` applies to a channel send only. A direct message has no scope of its own to give
— the radio floods it under the device default like anything else — so `--scope` with
`--to` is a usage error (exit `2`), as is a name the firmware would refuse. A radio that
can't send under the scope (firmware older than 1.10, or `*` before 1.16 with a default
scope set) refuses before anything is transmitted, and that is a failure (exit `1`),
never a quiet unscoped send. Quote the `*` so the shell leaves it alone:

```console
$ meshterm chat send "all clear" --channel 0 --scope '*' --json
{"kind":"channel","node":null,"channel":{"slot":0,"name":"#0","public":true,"hash":null},"sent":true,"acked":null,"scope":{"state":"unscoped","region":null,"code":null}}
```
`acked` is `null` on a channel, not `false`: there is no acknowledgement to have, and that
is exactly what one absence token is for — it is also why the plain face prints nothing
there, having no way to say it.

`history` is an array, oldest first:

```json
{"created_at":"2026-09-08T08:27:37Z","direction":"out","node":{"name":"Alice","key":null,"hash":"d4e5f6a7","type":null,"self":false},"channel":null,"snr_db":null,"text":"on my way","acked":true}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `created_at` | timestamp | |
| `direction` | string | `"in"` \| `"out"`. |
| `node` | object \| null | The *other* party, for a direct conversation. |
| `channel` | object \| null | The `channel` shape, for a channel conversation. |
| `snr_db` | number \| null | Reception SNR; `null` on an outbound message. |
| `text` | string | The body, raw and unescaped. |
| `acked` | boolean \| null | Delivery state of an outbound direct message; `null` inbound and on a channel. |

`list` is an array of conversations, channels first, each carrying `kind`, `channel`,
`node`, `unread`, `last_message_at` and `last_text`. `listen` is a stream of the same row
shape as `history`, with **`received_at`** in place of `created_at`:

```json
{"received_at":"2026-09-08T08:30:47Z","node":{"name":null,"key":null,"hash":"b2c3d4e5","type":null,"self":false},"channel":null,"snr_db":4.9,"text":"hello from Local-Repeater #1","direction":"in","acked":null}
```

An inbound message carries only the sender's key prefix, so its `node` is thinner than the
same node in `contacts` — `name` is whatever the resolver could find and `key` is `null`.
Join on `hash`.

`history`, `list` and `listen` return `5` when there is nothing to report.

#### `meshterm channels`

| Subcommand | What it does |
| --- | --- |
| `list` | The configured channel slots. |
| `add INDEX NAME [--secret HEX]` | Add a channel. A leading `#` makes it public (the firmware derives the key from the name); otherwise it is private, with a random key unless you give one. |
| `join INDEX NAME SECRET` | Join a channel from its name and 32-hex-character key. |
| `import INDEX URL` | Import a `meshcore://channel/add` link. |
| `share INDEX` | Print a slot's share link. |
| `clear INDEX --yes` | Clear a slot, removing the channel from the device. |
| `scope INDEX [REGION] [--clear]` | Show or set the region the channel's messages are flooded into. |

`list` prints `SLOT NAME TYPE HASH SCOPE`, one record per configured slot, and returns `5` when
the device has none. (The simulator these examples run against has none, so there is no
captured listing to show; each `--mock` invocation opens a fresh radio, and a slot written
by one run is not there for the next.)

```console
$ meshterm channels add 1 brigade
meshcore://channel/add?name=brigade&secret=2c70ca416304d520b60a28d9db35d6b3
```

`add` and `share` print the `meshcore://` URI alone, because the caller asked to be given
something to pass on. `join` and `import` print nothing: those two were *handed* the key,
and reading it back to them is not an answer. (The menu draws a QR code beside the link;
redirected into a file that is a block of block characters wrapped around the one thing
that is actually the answer.)

`clear` is gated behind `--yes`, because a private channel's key is lost with the slot
unless it is saved elsewhere.

`scope` is MeshTerm's to keep, not the radio's: the firmware has no per-channel scope, so
MeshTerm sets the companion's scope around each message it sends on the channel. Setting
one therefore writes nothing to the device, and it follows the channel to any slot it moves
to. With no `REGION`, `scope` prints the channel's region alone, the way `config get` prints
one value — and a channel with none prints `-` (`null` in `--json`) and exits `0`: it sends
under the device default, which is an answer. Setting (`scope 1 harbour`) or clearing
(`scope 1 --clear`) prints nothing plain. Reading an empty slot exits
`5`, like `share`; writing to one is a usage error (exit `2`), since there is no channel to
scope. `SCOPE` in `list` is `-` for a channel that sends under the device default.

**`--json`.** `list` is an array whose rows are **flat** — this is the one listing whose
record *is* the shared shape rather than carrying one, so a row is
`{"slot":0,"name":"Public","type":"public","hash":"11","scope":null}`, with `type` the plain column's
word rather than the `channel` shape's `public` boolean. Nesting a `channel` key under
every row would make `jq '.[].channel.name'` out of a listing whose every column is
already the channel.

All four writing subcommands emit the same document, each knowing the key it wrote:

```console
$ meshterm channels add 2 "#news" --json
{"channel":{"slot":2,"name":"#news","public":true,"hash":"03"},"secret":"ecadb1a7d803db8958bea1302ca6e8be","url":"meshcore://channel/add?name=%23news&secret=ecadb1a7d803db8958bea1302ca6e8be"}

$ meshterm channels clear 1 --yes --json
{"slot":1,"cleared":false}

$ meshterm channels share 1 --json
{"channel":null,"secret":null,"url":null}
```

`share` on an empty slot, and `clear` on one already empty, are exit `5` with the nulls
and `"cleared":false` — nothing to report, not a failure.

#### `meshterm courier`

A store-and-forward outbox: queue a message for a contact that is not reachable right now,
and it goes out the moment the contact is next heard — or at a time you name.

| Subcommand | What it does |
| --- | --- |
| `queue CONTACT TEXT… [--at HH:MM]` | Queue a message. `--at` holds it until the next occurrence of that local time. |
| `list` | The outbox: waiting entries then finished ones. |
| `send ID` | Force one delivery attempt now, skipping the "wait until heard" check. |
| `cancel ID` | Remove a waiting entry. |
| `clear` | Drop every finished (delivered / given-up) entry. |

```console
$ meshterm courier queue Alice "call me when you land" --at 18:30
id  1

$ meshterm courier list
ID  STATE    NODE   SCHEDULED                  FINISHED  TEXT
 1  waiting  Alice  2026-09-08T18:30:00-04:00         -  call me when you land
```

`queue` prints the entry's **id**, which is the one fact you have to keep: it is what
`send` and `cancel` take. An entry with no `SCHEDULED` time goes as soon as the contact is
heard.

`SCHEDULED` and `FINISHED` stay **absolute even without `--absolute`**. An appointment is
one of the places the instant *is* the fact: "18:30" is what you asked for and what will
happen, where "in 14h" is a restatement that goes stale while you read it.

The *sending* is done by a running interactive session's courier; a queued message sits in
the outbox until one runs. `courier send ID` is the way to force an attempt from a script.

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
`scheduled_at` and `text`; `send ID` answers `{"id":…,"outcome":…}`, where `outcome` is one
of `delivered`, `no ack`, `gave up`, `unknown contact`, `busy`, `gone` — kept verbatim,
spaces and all, so the two faces say the same word.

`list` returns `5` on an empty outbox; `cancel` and `clear` return `5` when there was
nothing to act on. A contact the device does not know, a contact with no key to address,
and an `--at` that is not a clock time are all **usage errors** (`2`): the argument is
wrong, nothing was queued, and nothing went out over the air.

---

### Tracing

#### `meshterm trace`

Walk the path to a target once and report what came back.

| Option | What it does |
| --- | --- |
| `-t`, `--target NAME` | **Required.** Target node name or key prefix. |
| `--path SPEC` | Force a route: comma-separated contact names and/or hex hashes, mixed freely (`3d,f2,3d`). Omit it and the device routes. |

`--target` is required, so `meshterm trace --path …` alone is a usage error — the
target-free form is [`trace-path`](#meshterm-trace-path).

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

Two blocks. The first is what the walk did, with the drawn `route` among the facts and the
typed `path` beside it — `path` reads `auto` when the device routed, and every other value
there is the comma-separated hex you can paste straight back into `--path`. The second is a
record per hop, naming its two ends by **hash** rather than repeating the names: the route
line above is where the names are, and the hash is what joins the two blocks.

A trace that never came home is **not** a failure of the command: the radio transmitted and
the walk ran. It reports `success no` and returns `5`.

#### `meshterm trace-path`

The other half of tracing: no target, just a route you compose by hand — out and back
whichever way you choose. It only has to end within earshot of this node.

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

`target` is `-` because a path walk has none. Walks record under a sentinel so they stay
out of the target picker's history.

**`--json`** is one object for both commands, the facts at the top level and the hop table
under `edges`.

| Field | Type | Meaning |
| --- | --- | --- |
| `target` | string \| null | The label the trace was addressed to. `null` for a path walk. |
| `path` | string \| null | The forced spec as hex, or `null` when the device routed (the plain face's `auto`). |
| `success` | boolean | Whether a reply came home. |
| `hops` | integer \| null | |
| `min_snr_db` | number \| null | The bottleneck hop's SNR. |
| `rtt_ms` | number \| null | |
| `tx_dbm` | integer \| null | TX power in effect when the trace ran, if known. **No plain column** — the facts block has no room. |
| `hash_bytes` | integer \| null | The per-hop hash width the walk addressed nodes at. Present when a path was forced. |
| `route` | array \| null | The `route` shape. `null` when nothing came home. |
| `edges` | array | The `edge` shape, one per hop, in walk order. `[]` when nothing came home. |

```console
$ meshterm trace-path --path "a1,d4" --json | jq -r '.edges | min_by(.snr_db) | "\(.from.hash) -> \(.to.hash)  \(.snr_db) dB"'
a1 -> d4  2.0 dB
```

#### `meshterm tx-optimize`

Sweep a remote node's transmit power against a target and pick the lowest level that still
gets through reliably.

| Option | What it does |
| --- | --- |
| `--path SPEC` | **Required.** Forced path ending at the target (`Repeater,Target`, or `3d,f2`). The hop before the target is the node being tuned. |
| `-n`, `--samples N` | Traces per TX level. Default 3. |
| `--step N` | Coarse sweep step. Default 3. |
| `--min N` / `--max N` | Bound the sweep (else the `tx_opt_min` / `tx_opt_max` preferences). |
| `--password TEXT` | Admin password for the tuned node; else the remembered one, else prompted. |
| `--apply` / `--no-apply` | Set the winner on the node. On by default. |

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

The winner comes first because it is what you asked for; the per-level records follow so
the choice can be checked against the measurements it was made from. This transmits many
times (paced by the `trace_cooldown_s` preference) — it is the one command that is not a
single transmission.

**`--json`** puts the levels under `levels` and upgrades the two bare names to `node`
objects, which matters most here: the two nodes are told apart by nothing else on the line.

```json
{"tuning_node":{"name":"Yagi-Repeater","key":"a1b2c3d400000000000000000000000000000000000000000000000000000000","hash":"a1b2c3d4","type":"repeater","self":false},"target":{"name":"Alice","key":"d4e5f6a700000000000000000000000000000000000000000000000000000000","hash":"d4e5f6a7","type":"companion","self":false},"path":"a1b2c3d4,d4e5f6a7","optimal_tx_dbm":19,"target_snr_db":8.7,"reliability":1.0,"previous_tx_dbm":20,"applied":true,"levels":[{"tx_dbm":18,"target_snr_db":6.6,"successes":2,"samples":2},{"tx_dbm":19,"target_snr_db":8.7,"successes":4,"samples":4},{"tx_dbm":20,"target_snr_db":9.1,"successes":2,"samples":2},{"tx_dbm":21,"target_snr_db":7.6,"successes":2,"samples":2},{"tx_dbm":24,"target_snr_db":4.0,"successes":2,"samples":2},{"tx_dbm":27,"target_snr_db":-9.9,"successes":2,"samples":2},{"tx_dbm":28,"target_snr_db":-15.1,"successes":1,"samples":2}]}
```

`reliability` is a fraction in `[0, 1]`, not a formatted `"1.00"`. `levels` is ordered by
`tx_dbm` ascending, as the plain table is; the menu's star on the winning row has no column
here, because `optimal_tx_dbm` names it.

With no password available and nowhere to ask for one, this is a **usage error** (`2`) —
nothing was transmitted. A password that is refused is a device error (`4`), and the saved
one is cleared. Returns `5` when no trace reached the target at any level: nothing was
measured, and the node was left at its original power.

---

### Other nodes

#### `meshterm repeater-admin`

Send one command to a remote repeater or room server over the mesh and print its reply.

```
meshterm repeater-admin NODE COMMAND... [--password TEXT]
```

| Argument / option | What it does |
| --- | --- |
| `NODE` | The remote contact's name. |
| `COMMAND...` | The text command to send, as the node's own CLI spells it. |
| `--password TEXT` | Admin password; else the one remembered from an interactive login. |

```console
$ meshterm repeater-admin Yagi-Repeater get name --password admin
> Yagi-Repeater
```

The reply is printed as the node sent it — its own text, and the whole answer, echoed
prompt and all. A node that does not answer is not a failure (the command may well have
landed), but there is nothing to report, so it returns `5`.

Logging in happens automatically from the remembered password; run the interactive flow
once to store one, or pass `--password`. A refused login clears the saved password and
fails with `4` — the node answered, and answered no. An **absent** password, and a node
name no contact matches, are usage errors (`2`): nothing was transmitted in either case,
which is the line `3` and `4` are drawn on.

**`--json`:**

```json
{"node":{"name":"Yagi-Repeater","key":"a1b2c3d400000000000000000000000000000000000000000000000000000000","hash":"a1b2c3d4","type":"repeater","self":false},"command":"get name","reply":"> Yagi-Repeater"}
```

`reply` is the node's own text, whole and verbatim — raw, unescaped, multi-line where the
node sent multiple lines. It is **not** parsed: MeshTerm does not know the remote node's
CLI grammar, and a document that pretended to would be inventing structure. `reply` is
`null` on the exit-`5` no-answer path.

#### `meshterm regions`

Ask a repeater which regions it relays floods for (firmware 1.12 or newer).

```
meshterm regions NODE
```

| Argument / option | What it does |
| --- | --- |
| `NODE` | The repeater's contact name. |

```console
$ meshterm regions Yagi-Repeater
node      Yagi-Repeater
unscoped  yes
regions   lakeside, lakeside-north, harbour
```

One anonymous request — no login — and one transmission. A repeater answers it only when it
arrives direct: from a neighbour, or over a route the companion has learned. It also
rate-limits the question, so nothing here retries it. `unscoped` says whether it relays plain
floods too; `regions` lists the regions it relays *scoped* floods for. The answer is
remembered, so the node page and the packet views know it afterwards.

A repeater that answered and named nothing at all relays no floods, and returns `5`. One that
never answered is a device error (`4`). A name no contact matches, and a contact known to be
something other than a repeater, are usage errors (`2`): nothing was sent.

**`--json`:**

```json
{"node":{"name":"Yagi-Repeater","key":"a1b2c3d400000000000000000000000000000000000000000000000000000000","hash":"a1b2c3d4","type":"repeater","self":false},"unscoped":true,"regions":["lakeside","lakeside-north","harbour"]}
```

`regions` never holds the wildcard `*` — that is `unscoped`, since it names no region.

---

### MeshTerm itself

#### `meshterm preferences`

How MeshTerm behaves, as opposed to how the radio is configured. Reads nothing from the
companion, transmits nothing, and works with no device attached. With no subcommand it
runs `show`.

| Subcommand | What it does |
| --- | --- |
| `show` | Every preference, its value, its built-in default, and what it does. |
| `get KEY` | One preference's value, bare. |
| `set KEY VALUE` | Change one preference. |
| `reset --yes` | Return every preference to its default. |

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

**`DESCRIPTION`** says what each preference does, because a key's name is not its
meaning. Values print in the form `preferences set` accepts back — no unit suffix on a
number, `on`/`off` for a boolean. Where `VALUE` differs from `DEFAULT`, this install has an
override.

Overrides live in `preferences.toml` in [MeshTerm's state
directory](#where-meshterm-keeps-its-state), which lists only what you have changed; delete
a line and the default takes over again.

**`--json`** is an array of `setting` objects carrying `default` and `overridden`:

```console
$ meshterm preferences get trace_cooldown_s --json
{"key":"trace_cooldown_s","value":5.0,"type":"float","label":null,"redacted":false,"default":5.0,"overridden":false}
```

`overridden` is what the plain face makes the reader derive by comparing two columns.
`DESCRIPTION` has no JSON counterpart — it is prose for a person, and a consumer that
wanted it would be rendering a settings screen. A boolean prints `on`/`off` plain and is
`true`/`false` here, which is the one type whose round-trip through `preferences set` needs
a mapping; every other `.value | tostring` goes straight back in.

`set` and `reset --yes` answer `{"changes":…,"applied":[…],"path":…}`, `path` being the
`preferences.toml` that was written.

#### `meshterm about`, `about-author`, `discord`, `support`

The four written pages, printed as plain text. `about` is what MeshTerm is and the terms it
ships under; `about-author` is who wrote it; `discord` is the community invite; `support`
is what keeps it going.

```console
$ meshterm discord
Join the Discord
  Questions, ideas, bug reports, and mesh talk.

  • https://discord.gg/AZwe5Uvb3S
```

These four are the one place the CLI wraps, at 72 cells. The menu draws a QR code beside
each link; the scripted face prints the link alone.

**`--json`:**

```json
{"page":"discord","title":"Join Discord","version":"0.9.0","text":"Join the Discord\n  Questions, ideas, bug reports, and mesh talk.\n\n  • https://discord.gg/AZwe5Uvb3S","links":["https://discord.gg/AZwe5Uvb3S"]}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `page` | string | `"about"` \| `"about-author"` \| `"discord"` \| `"support"`. |
| `title` | string | The page's own title. |
| `version` | string | The version the page's `{version}` placeholder resolved to. |
| `text` | string | The whole page as the plain face prints it, newlines and all, raw. |
| `links` | array of strings | Every URL the page carries, in document order. |

`links` is the one machine-actionable thing on a prose page, and the one thing the menu
bothers to draw a QR of. `text` is the page's plain rendering, not a fold of the markdown
into nested JSON — a structured markdown tree would be a second rendering rather than data,
and nothing would consume it.

#### `meshterm diagnostics`

Everything a bug report opens with, in one block: which MeshTerm, on which host, in which
terminal, talking to which radio, over how much stored history. It is the answer to the
three or four questions that otherwise get asked one at a time in an issue thread, and the
person who hit the bug is usually the one least able to answer them — half of it is
resolved at boot and shown on no screen.

This sample is the one exception to [About the examples](#about-the-examples): it is an
illustrative report from a real radio over Bluetooth, because the simulator's report says
little. The field names and layout are what the command prints; the values are examples.

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

The seven groups are the page's seven sections — build, host, terminal, radio, storage,
stored rows, changed preferences. The command line shows them as blank-line-separated
records, because that face is a stream to grep; the page and the saved file show them as
markdown headings, because that one is a document to read.

**The radio is asked but never required.** A report about a companion that will not connect
is exactly the report most worth filing, so a failure to reach it becomes `connected no`
with the radio's own words in `error`, and every other fact still arrives.

**Only overridden preferences are listed.** The defaults are in the source and identical for
everyone; the two the reporter changed are the two that can explain anything.

**The mesh is described in aggregate and only in aggregate** — a row count per table, and
the span of time the observations cover. The counts come off the schema rather than a list
written down somewhere, so a table added later starts being reported the day it lands.

**Nothing in the block is private.** No pairing PIN, no admin password, no channel secret,
no private key, no position, and no contact's name or key. That is a property of the
feature and not a habit: the block is designed to be pasted in public by somebody who has
not read it, and `tests/test_diagnostics.py` plants real secrets in every store that holds
one and fails if any of them — or a field merely *named* like one — reaches either face.

Under `--json`, the five fact blocks merge into one flat object and the two listings keep
a key of their own:

```json
{"meshterm":"0.9.0","install":"frozen","os":"Windows 11","…":"…","tables":[{"table":"app_state","rows":4},"…"],"preferences":[{"preference":"log_level","value":"DEBUG"},{"preference":"history_days","value":"730"}]}
```

**`--out PATH` writes it to a file instead of printing it**, and the answer becomes the
path — the same trade `config export-key --out` makes, since a caller who asked for a file
wants to be told where it is rather than handed the contents they just redirected into it.

```console
$ meshterm diagnostics --out meshterm-diagnostics.md
✓ diagnostics written — attach it to the report.
meshterm-diagnostics.md
```

**The file is markdown**, whichever face the run was printing, because an issue tracker
renders markdown as written — no fence, no apology. Every value is a code span, so a
Windows path's backslashes and a firmware error's asterisks arrive as themselves rather
than as markdown. Redirection still works and is not replaced; `--out` is what gives you
the document rather than the record stream.

In the menu this is the **Diagnostics** page under *This app*, and it is one of the app's
**written pages** — the same markdown renderer behind the About pages, drawing the same
document the file holds. That buys what a long block most needs: its `##` headings are
landmarks, so each pins to the top row while its own section scrolls under it and
`^PgUp`/`^PgDn` step section by section.

`s` saves, advertised in the footer hint on a desktop and on the lane's free **F3** slot on
the PicoCalc — never in the page's body, which belongs to the report. It writes
`meshterm-diagnostics.md` into the config directory beside the log, and answers in a popup
naming the path. A write that fails says so in the same popup, in red, and leaves the page
readable.

#### `meshterm platform`

The narrow sibling of `diagnostics`: which UI flavour a given invocation resolves to, and
why, without asking the radio anything.

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

`icons` is a separate question from the flavour, and the usual reason a Windows session
looks plainer than the screenshots. Under `--json` the same keys, with `icons` a boolean:

```json
{"platform":"regular","platform_source":"default","flag":null,"env":null,"device_tree_model":null,"icons":true,"icons_source":"no-console"}
```

#### `meshterm specimen`

Print the visual-language specimen — every mark, icon, colour scale and fold on one card.

**This is the one command that keeps its colour**, and it builds its own themed console to
do it. That is deliberate: the colour *is* the output. It is the font-and-palette
acceptance card on the PicoCalc console, and with `--platform picocalc` it previews that
flavour from a desktop. A monochrome specimen would test nothing.

It is also the one command that **refuses `--json`**, as a usage error (exit `2`). There is
no data face behind a colour card, and a document of it would be a lie or an empty gesture.

---

## Recipes

Structure goes through `--json` and `jq`; the plain face is for looking at.

**Every repeater's key.**

```bash
meshterm contacts --json | jq -r '.[] | select(.node.type == "repeater") | .node.key'
```

**Name and last-heard, tab-separated**, for a spreadsheet or `column -t`:

```bash
meshterm contacts --json | jq -r '.[] | [.node.name, .heard_at] | @tsv'
```

**Watch a node and shout if it goes quiet.** `trace` returning `5` means the walk ran and
nothing came home.

```bash
#!/usr/bin/env bash
target=${1:?usage: watch NODE}
meshterm trace --target "$target" > /dev/null
status=$?          # capture it first: `if ! cmd` would have reset $? to 0
if [ "$status" -eq 5 ]; then
    echo "$target did not answer" | mail -s "mesh alert" me@example.com
fi
```

**The bottleneck hop of a walk**, which is the number a route argument is usually about:

```bash
meshterm trace-path --path "a1,d4" --json \
  | jq -r '.edges | min_by(.snr_db) | "\(.from.hash) -> \(.to.hash)  \(.snr_db) dB"'
```

**A route as one line**, our own node included, the way the plain face draws it:

```bash
meshterm trace-path --path "a1,d4,a1" --json \
  | jq -r '.route | map(.name // .hash) | join(" -> ")'
```

**Capture a window of traffic and aggregate it yourself.** The plain face's summary block
has no JSON counterpart on purpose; the records it was computed from are all there.

```bash
meshterm monitor --seconds 300 --json > capture.ndjson
jq -s -c 'group_by(.node.hash)[]
    | {hash: .[0].node.hash,
       name: (map(.node.name) | map(select(. != null)) | first),
       packets: length,
       best_snr_db: (map(.snr_db) | max)}' capture.ndjson
```

**One value into a variable.** `get` prints the bare value because you named the key:

```bash
sf=$(meshterm config get radio_sf)
[ "$sf" -lt 9 ] && meshterm config set radio_sf 9
```

**Nightly config archive**, keeping the path the command actually wrote:

```bash
out=$(meshterm config backup "$HOME/mesh/$(date +%F).toml") && echo "archived $out"
```

**Queue for someone who is offline**, then chase it later:

```bash
id=$(meshterm courier queue Alice "call me" --json | jq -r .id)
# ... later ...
meshterm courier send "$id"
```

**Sample a trace politely.** A trace transmits exactly once per invocation by design; pace
the repeats yourself:

```bash
for i in $(seq 5); do
    meshterm trace --target Alice --path "3d,f2,3d"
    sleep "$(meshterm preferences get trace_cooldown_s)"
done
```

**Let MeshTerm advertise, rather than cron.** A companion never advertises by itself, so
MeshTerm can do it for you — once a week, and only when the air is quiet:

```bash
meshterm preferences set weekly_flood_advert on   # starts the week; sends nothing now
meshterm preferences set advert_quiet_s 60         # wait for a minute of silence first
```

A running MeshTerm session then floods one advert from a device once it has gone a full
week without one — a flood advert you send by hand restarts that device's week — and only
after it has heard the mesh and then heard nothing for the quiet spell plus a random 0–5 s.
**Do not put `config advert --flood` in a cron slot instead**: every repeater in range
rebroadcasts a flood advert, and a repeater that decides you are bursting can blacklist
you.

**Cron-friendly quiet.** `--quiet` suppresses console logging entirely, leaving only the
command's own output and any error line. Cron's `PATH` is nearly empty, so give it the
absolute path:

```cron
0 * * * * /usr/local/bin/meshterm --quiet --profile yagi contacts --json > /var/log/mesh/contacts.json
```

---

## What has no command

Some features have no scripted face, and that is the honest answer rather than a degraded
one. Each of these is a *live picture* — its meaning is where things sit on a grid, how
they move, and what colour they are — and none of that survives being turned into records.

| Feature | Why not, and what to use instead |
| --- | --- |
| **Map** | A drawing of braille cells whose nodes are told apart by colour. Stripped of colour to match the rest of the CLI it would be unreadable; left coloured it could not be piped anywhere useful. `meshterm contacts` lists the same nodes, coordinates and all. |
| **Dashboard** | A live overview that repaints every second. `meshterm monitor` captures the same traffic as records. |
| **Live feed** | Every packet as it arrives, newest first, with a viewer behind each row. `meshterm monitor` is the scripted tail. |
| **Watchtower** | A background sentinel over starred nodes; it exists to *interrupt* a session. Build the same alarm from `meshterm trace`'s exit status (see [Recipes](#recipes)). |
| **Time machine** | Braille charts over a switchable window. The underlying history is in the database `--db` points at. |
| **Mesh walk** | An evidence graph walked one node at a time. `meshterm records` and `meshterm trace` cover the walks it is built from. |

Everything else in the menu has a command, listed above — and every one of them answers to
`--json`.
