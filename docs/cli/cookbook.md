# The CLI cookbook

Common one-liners, in groups by what you want to do. [The command line](README.md) is the
full manual. It has each command, each option, both output faces, and the exit statuses.
Its own [Recipes](README.md#recipes) section goes further than this page, with `jq`
pipelines and shell loops.

Nearly each menu option is also a subcommand. This is good for scripts, cron, and bots.
The live pictures stay in the menu, because their meaning is there (the map, dashboard,
live feed, watchtower, and mesh walk). Each other part has a command, and each command
accepts `--json`. **[`docs/cli.md`](README.md) is the full manual.** It has each command
and option, what each one prints on both faces, and the exit statuses. These are some
examples:

```bash
# Trace: a trace transmits exactly once. Run it again to get more samples.
meshterm trace --target Alice --profile yagi

# Force a route through specific repeaters (as in the MeshCore app). Give comma-separated
# contact names or hex key prefixes, in any mix. If you leave it blank, the device routes.
meshterm trace --target Alice --path "3d,f2,3d"
meshterm trace --target Alice --path "3d,Bravo-Repeater,f2"

# Walk a composed circuit with no target at all: out and back, in your own way
meshterm trace-path --path "3d,f2,3d"

# Sweep and apply the TX power of a remote node
meshterm tx-optimize --path "Bravo-Repeater,Alice" --samples 6 --step 3 --apply

# Messaging: the live transcript is a menu screen. The CLI takes a subcommand.
meshterm chat send --to Alice "on my way"      # direct message
meshterm chat send --channel 0 "net in 5"      # channel broadcast
meshterm chat history --to Alice
meshterm chat list

# Store and forward: queue a message for a contact that is offline now
meshterm courier queue Alice "ping me when you're back" --at 18:30
meshterm courier list                 # the outbox — waiting and finished
meshterm courier send 3               # force one delivery attempt now

# Remote repeater admin (one transmission for each run). You need --password
# until an interactive login stores one.
meshterm repeater-admin Bravo-Repeater "get name" --password YOUR-ADMIN-PASSWORD

# Device configuration: show, set, back up, restore
meshterm config                       # show all current settings, one `key value` per line
meshterm config get name              # just the value, ready for $(...)
meshterm config set radio_sf 9        # change one setting
meshterm config backup node.toml      # archive every setting to TOML
meshterm config restore node.toml --dry-run
meshterm preferences set weekly_flood_advert on   # flood an advert once a week, when quiet

# Passive capture window (records to the history, transmits nothing)
meshterm monitor --seconds 60
```

You can type the global options before or after the subcommand. The global options are
`--profile/-p`, `--port`, `--ble`, `--ble-pin`, `--tcp`, `--spi`, `--mock`, `--db`,
`--json`, `--absolute`, `--quiet/-q`, and `--platform`. For example,
`meshterm contacts --json` and `meshterm --json contacts` are the same run.

## Two output faces

*(This is the short version. [`docs/cli.md`](README.md) has all of it.)*

The **plain face** is for a person at a prompt. It prints like a standard Unix utility.
It has no colour, no borders, and one record on each line. Nothing wraps, except the four
written pages (`about`, `about-author`, `discord`, `support`), which wrap at 72 cells. It
is easy to read. Listings are `ps`-style aligned records with **bare names** (alignment
is the delimiter). Times are **relative ages** (`now`, `5m`, `never`). A route is drawn
with arrows: `MockCompanion (00) → Yagi-Repeater (a1) → Alice (d4)`. `--absolute` changes
each age back to an ISO-8601 instant. A *path* is the spec that `--path` takes as input,
and it stays comma-separated hex. `-` is the one token for absent. Errors,
acknowledgements, and progress all go to stderr, so a redirect catches only the answer.

The **JSON face** is the machine contract, and `--json` works on **each** command:

```console
$ meshterm contacts --json | jq -r '.[] | select(.node.type == "repeater") | .node.key'
b2c3d4e500000000000000000000000000000000000000000000000000000000
a1b2c3d400000000000000000000000000000000000000000000000000000000
```

There is no envelope. A listing is an array, and a set of facts is an object. The output
is one compact line. `monitor` and `chat listen` stream one document for each record.
Values are typed. Absent is `null` and never an omitted key. Timestamps are always UTC to
the second, with or without `--absolute`. `--json` changes the rendering and never the
report. The records are the same, and the exit status is the same.

For structured data, use `--json` and `jq`. The plain face is for you to look at.

## Exit status

These are the exit statuses:

- `0`: success.
- `1`: failure.
- `2`: usage error.
- `3`: no device found (nothing was transmitted).
- `4`: MeshTerm reached the device, but the operation failed.
- `5`: nothing to report.

`meshterm --help` prints the table with the full wording. [`docs/cli.md`](README.md#exit-status)
explains it.

Learn `5` first. It lets a script tell "found nothing" from "worked" without a count of
the lines of output.

```bash
if meshterm contacts > contacts.txt; then
    echo "$(($(wc -l < contacts.txt) - 1)) contacts"      # minus the header
elif [ $? -eq 5 ]; then
    echo "no contacts yet"
fi
```

