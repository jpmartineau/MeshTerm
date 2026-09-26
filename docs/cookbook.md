# The CLI cookbook

Common one-liners, by the thing you are trying to do. [The command line](cli.md) is the
full manual — every command, every option, both output faces, the exit statuses — and its
own [Recipes](cli.md#recipes) section goes further than this one, into `jq` pipelines and
shell loops.

Nearly every menu option is also a subcommand — ideal for scripting, cron, and bots. The
live pictures stay in the menu, where their meaning is (the map, dashboard, live feed,
watchtower, and mesh walk); everything else has a command, and every command speaks
`--json`. **[`docs/cli.md`](cli.md) is the full manual**: every command and option,
what each one prints on both faces, and the exit statuses. A taste:

```bash
# Trace — a trace transmits exactly once; run it again to sample more.
meshterm trace --target Alice --profile yagi

# Force a route through specific repeaters (MeshCore-app style): comma-separated
# contact names and/or hex key prefixes, mixed freely. Blank lets the device route.
meshterm trace --target Alice --path "3d,f2,3d"
meshterm trace --target Alice --path "3d,Bravo-Repeater,f2"

# Walk a composed circuit with no target at all — out and back your own way
meshterm trace-path --path "3d,f2,3d"

# Sweep and apply a remote node's TX power
meshterm tx-optimize --path "Bravo-Repeater,Alice" --samples 6 --step 3 --apply

# Messaging: the live transcript is a menu screen; the CLI takes a subcommand
meshterm chat send --to Alice "on my way"      # direct message
meshterm chat send --channel 0 "net in 5"      # channel broadcast
meshterm chat history --to Alice
meshterm chat list

# Store-and-forward: queue for a contact that's offline right now
meshterm courier queue Alice "ping me when you're back" --at 18:30
meshterm courier list                 # the outbox — waiting and finished
meshterm courier send 3               # force one delivery attempt now

# Remote repeater admin (one transmission per invocation). --password is needed
# until an interactive login has stored one.
meshterm repeater-admin Bravo-Repeater "get name" --password YOUR-ADMIN-PASSWORD

# Device configuration: view, set, back up, restore
meshterm config                       # show all current settings, one `key value` per line
meshterm config get name              # just the value, ready for $(...)
meshterm config set radio_sf 9        # change one setting
meshterm config backup node.toml      # archive every setting to TOML
meshterm config restore node.toml --dry-run
meshterm preferences set weekly_flood_advert on   # flood an advert once a week, when quiet

# Passive capture window (records to history; transmits nothing)
meshterm monitor --seconds 60
```

Global options — `--profile/-p`, `--port`, `--ble`, `--ble-pin`, `--tcp`, `--spi`, `--mock`,
`--db`, `--json`, `--absolute`, `--quiet/-q`, `--platform` — may be typed **before or
after** the subcommand: `meshterm contacts --json` and `meshterm --json contacts` are the
same run.

## Two output faces

*(The short version — [`docs/cli.md`](cli.md) has the whole of it.)*

The **plain face** is for a person at a prompt. It prints like a standard Unix utility —
no colour, no borders, one record per line, nothing wrapped except the four written
pages (`about`, `about-author`, `discord`, `support`), which wrap at 72 cells — and it is
allowed to be comfortable about it: listings are `ps`-style aligned records with **bare names**
(alignment is the delimiter), times are **relative ages** (`now`, `5m`, `never`), and a
route is drawn with arrows — `MockCompanion (00) → Yagi-Repeater (a1) → Alice (d4)`.
`--absolute` swaps every age back for an ISO-8601 instant. A *path*, the spec `--path`
takes back, stays comma-separated hex. `-` is the one token for absent. Errors,
acknowledgements, and progress all go to stderr, so a redirect catches only the answer.

The **JSON face** is the machine contract, and `--json` works on **every** command:

```console
$ meshterm contacts --json | jq -r '.[] | select(.node.type == "repeater") | .node.key'
b2c3d4e500000000000000000000000000000000000000000000000000000000
a1b2c3d400000000000000000000000000000000000000000000000000000000
```

No envelope — an array for a listing, an object for a set of facts. One compact line;
`monitor` and `chat listen` stream one document per record. Values are typed, absent is
`null` and never an omitted key, and timestamps are always UTC to the second regardless of
`--absolute`. `--json` changes the rendering, never the report: same records, same exit
status.

Anything structural should go through `--json` and `jq`. The plain face is for looking at.

## Exit status

`0` success · `1` failure · `2` usage error · `3` no device found (nothing was
transmitted) · `4` the device was reached but the operation failed · `5` nothing to
report. The table with its full wording is printed under `meshterm --help` and explained
in [`docs/cli.md`](cli.md#exit-status).

`5` is the one worth knowing about: it lets a script tell "found nothing" from "worked"
without counting output lines.

```bash
if meshterm contacts > contacts.txt; then
    echo "$(($(wc -l < contacts.txt) - 1)) contacts"      # minus the header
elif [ $? -eq 5 ]; then
    echo "no contacts yet"
fi
```

