---
name: profile-picocalc
description: Measure MeshTerm's real keystroke latency on the PicoCalc — drive the app on the device's own console, time every key, and verify what actually reached the panel. Use when asked to profile, benchmark, or speed up the TUI, when a screen "feels slow", or to A/B a rendering/perf change on hardware.
---

# Profiling MeshTerm on the PicoCalc

Measures the thing that actually matters: the wall time between a keystroke and the panel
showing its result, on the real device, against the real radio and the real database.

**Synthetic benches mislead here.** Gallery stubs hold two contacts where the device holds
141; the desktop's CPU hides costs that dominate on a Cortex-A7. Every number worth acting
on came from the real app on `/dev/tty1`.

## Reaching the device

The address, login, key and password are **not in this file**. Read `.dev.env` at the
repo root first (gitignored; `.dev.env.example` shows the shape) — the `$DEV_*` names
below are its keys, and everything here is written to be run with them substituted.

`ssh -i $DEV_PICOCALC_SSH_KEY $DEV_PICOCALC_USER@$DEV_PICOCALC_HOST` (key installed;
`scp` with the same flags pushes files byte-exact). Password fallback:
`plink -batch -pw "$DEV_PICOCALC_PASSWORD"`. `sudo` needs the password piped:
`echo "$DEV_PICOCALC_PASSWORD" | sudo -S …`.

The repo lives at `$DEV_PICOCALC_REPO`, venv at `.venv`. Push changed modules with `scp`
straight into the tree — no reinstall, it is a `pip install -e .`.

**One-time**: reading the console back needs the `tty` group. Busybox's `addgroup $DEV_PICOCALC_USER tty`
misparses; edit `/etc/group` instead (the trailing name is the deploy user):

```sh
echo "$DEV_PICOCALC_PASSWORD" | sudo -S sed -i 's/^tty:\(.*\):\(.*\)$/tty:\1:\2,'"$DEV_PICOCALC_USER"'/' /etc/group
```

Re-open the SSH session for it to take effect. Verify: `dd if=/dev/vcs1 bs=1378 count=1 | wc -c`
should print `1378` (53×26).

## Running a tour

```sh
scp -i $DEV_PICOCALC_SSH_KEY scripts/drive_console.py tours/nav_all.txt "$DEV_PICOCALC_USER@$DEV_PICOCALC_HOST:$DEV_PICOCALC_REPO/"
ssh -i $DEV_PICOCALC_SSH_KEY "$DEV_PICOCALC_USER@$DEV_PICOCALC_HOST" "cd $DEV_PICOCALC_REPO && .venv/bin/python drive_console.py \
    --script nav_all.txt --boot-wait 20 --shots ~/tmp/shots"
```

`drive_console.py` forks a pty sized to the panel, runs MeshTerm inside it, mirrors every
byte to `/dev/tty1` so the screen shows exactly what a person would see (JP can follow
along live), injects the scripted keys, and prints p50/p90/max per key group.

Tour scripts are one step per line: `down 8`, `enter`, `text:y`, `sleep 3`,
`label: contacts` to start a new measurement group, `shot NAME` to grab the screen,
`burst right 12 100` to send keys at a fixed rate without waiting (see below).
`tours/` has `nav_all.txt` (every page), `nav_ab.txt` (short, for A/B runs),
`nav_opens.txt` (screen opens, timed with `expect`), `nav_map.txt`, `map_burst.txt`
(the map driven faster than it can keep up), and
`nav_changed.txt` (every optimised paint plus a real dialog — the A/B tour).

## A/B'ing a change: revert the device, don't rebuild it

The device tree is a git checkout, so the cleanest before/after is to `scp` the changed
modules in, run the tour, then `git checkout -- meshterm/` and run it again. Same panel,
same database, same radio, minutes apart — nothing else gets that close. Drop the
`__pycache__` between runs (`find meshterm -name __pycache__ -type d -exec rm -rf {} +`)
and remember to push the files back afterwards.

Then diff the shots (`cmp -s before/NN_x.txt after/NN_x.txt`). Screens carrying live data
(the header's counters, a heard-sorted contact list) legitimately differ; a screen whose
content is fixed for the run — a dialog over the menu, the mesh walk's settled graph — must
come back **identical**, and that is what proves a rendering change only changed the time.

## Timing a screen *open* — use `expect`, not silence

A repaint is one burst of output, so "silent for 45 ms" cleanly ends it. An **open** is not:
it paints the menu closing, falls silent for however long the tool takes to load and build,
then paints the new screen. At the repaint threshold the clock stops on that first burst and
reports an open that never happened (the mesh walk read as 124 ms when it truly took 4.9 s).
Widening the window with `--quiet-ms` does not rescue it either — past about a second the
2 s header tick lands inside every measurement and inflates all of them.

So watch the panel instead:

```
label: open-walk
enter
expect  links
```

`expect TEXT` polls `/dev/vcs1` until `TEXT` is on screen and reports the time since the
last key. **The needle must be unique to the destination screen.** Every main-menu row
carries its screen's name, so `expect Mesh walk` and `expect Contacts` match the *menu* and
return instantly — a silent false pass that made a 2.3× improvement look like no change.
Use something only the opened screen draws: `links` for the walk (`… 334 nodes · 1145 links`),
` known` for Contacts (`Contacts · 141 known`), `mesh overview` for the Dashboard.

**The app boots to a device picker** — it does not auto-connect. A tour must press `enter`
on the splash and then `sleep 25` for the radio, or everything after it measures the splash.
Check `shots/*_boot.txt` if the numbers look uniform and wrong.

## Reading the console back

`--shots DIR` dumps `/dev/vcs1` (the console's screen memory — exactly `rows*cols` bytes of
text) after each group. This is the **only** trustworthy way to compare two renderers: the
text is what the panel holds. Do not try to replay the ANSI stream yourself.

Diff two runs with `cmp -s`. Expect legitimate differences from live data — ages, packet
counts, the unread badge, and Contacts re-sorting when a node is heard. Compare a screen
whose content is static (the main menu) for a true equality check.

## Stage breakdown inside the app

When a page is slow and you need to know *which stage*, run the app under `instrument.py`
instead of the binary:

```sh
ssh … "cd $DEV_PICOCALC_REPO && .venv/bin/python drive_console.py --script nav_all.txt \
    --exe $DEV_PICOCALC_REPO/run_instrumented.sh --boot-wait 20"
```

where `run_instrumented.sh` is a two-line wrapper `exec .venv/bin/python instrument.py "$@"`.
It appends a CSV row per repaint to `~/tmp/keytrace.csv` on the device: handle, header, compose,
dialog, parse, paint, total, and the `render_to_ansi` call count. Rows tagged `~tick` are
idle repaints — what the app burns doing nothing.

It hooks `Application._redraw`, deliberately, because MeshTerm swaps in its own
`FastRenderer` for plain frames: patching `Renderer.render` sees only the paints that fall
back to prompt_toolkit, and reports a handful of dialog repaints as if they were the whole
session. If a trace comes back suspiciously sparse, that is the first thing to check.

**Read the row *after* an action, not just the action's own row.** Leaving a screen is a
good example: the `escape` row is ~10 ms, and the menu that replaces it repaints ~46 ms
later as an untriggered `~tick` (the menu loop resumes asynchronously, so it is not
attributed to the key). Judging Esc by its own row alone understates it; judging it by
console quiescence overstates it — the truth was ~90 ms, from the sequence.

## When the app falls behind, or freezes

The per-key clock waits for each key to finish, so it can never see the failure a fast thumb
causes: keys arriving faster than the app answers them. `burst KEY N MS` sends N presses MS
apart regardless, then reports the **catch-up** (last key sent to the app quiet for 1.5 s)
and the **longest gap** in its output. A long catch-up with short gaps is the app working
through a backlog while staying live; a long gap is a freeze.

To see *why*, run the app under `scripts/watchdog.py` (`--exe .../run_watchdog.sh`, with
`watchdog.py` copied to the repo root like `instrument.py`). A thread outside the event loop
pings it every 50 ms and, while a ping goes unanswered, samples every thread's stack; twice a
second it logs RSS, swap and major page faults. Two signatures:

- **The log itself goes silent** for seconds and `majflt` jumps by thousands: the whole
  process was stopped while the kernel paged it back in from the SD-card swap. That is
  memory, not CPU — find what grew (`rss`, `swap`, the map's `memo=` bytes).
- **Pings of 0.3–1 s with the main thread somewhere cheap** — a `glob`, a `stat`, a small
  read: the GIL convoy. Each syscall hands the GIL back and waits up to 5 ms to retake it
  while a worker thread (a raster, a tile decode) holds it, so a loop doing hundreds of small
  reads takes hundreds of times longer. Anything that walks the filesystem belongs in
  `asyncio.to_thread`, however cheap it looks idle.

The map persists its view, so pin it before every run or two runs start in different places:
`INSERT OR REPLACE INTO app_state(key, value) VALUES ('map_view', '{"lat": 45.52, "lon":
-73.62, "zoom": 10}')` into `~/.meshterm/meshterm.db`, with the venv's Python (the image's
own has no `sqlite3`). And `expect` needles must be ASCII: `/dev/vcs1` holds font glyph
indices, so `·` never matches — `km across` finds the map's title.

## Traps that cost real time

- **cProfile lies on this CPU.** Its per-call overhead (~1.9×) wrecks a path made of tens of
  thousands of tiny calls. It blamed prompt_toolkit's `split_lines`/`get_cursor_position`,
  and a control that removed both was a *wash*. Use **ablation** — remove one stage from a
  real render loop and read the difference. cProfile is fine for finding a hot *function*
  when the calls are few and large (it found the map's per-vertex reprojection instantly).
- **`get_app()` outside a running app** builds a throwaway `DummyApplication`, which reloads
  the whole vi/emacs binding set — ~150 ms *per call*. Always wrap a manual
  `renderer.render()` in `with set_app(app):` or you profile that instead. Symptom:
  `load_vi_bindings` at the top of the profile.
- **Drain pending output before each key.** Without it a slow app's tail lands on the next
  key's clock and reads as a bogus 0.3 ms. `drive_console.py` does this; anything hand-rolled
  must too.
- **A silent exception stops instrumentation dead.** An error inside a `finally` in
  `Renderer.render` is swallowed by asyncio and logged to `~/.meshterm/meshterm.log.jsonl`,
  not the screen. Wrap logging bodies in try/except that writes the traceback to a file.
- **Busybox**: `head -3` is invalid, use `head -n 3` (or `sed -n 1,3p`). No `timeout`.
- **`git add -A` in the repo root** will sweep up any tour/bench file left there.

## The hardware floor

The panel is an ILI9488 over SPI at 80 MHz, `fps = 30` in the device tree. A full
320×320×16bpp flush is ~20 ms of pure SPI, and DRM coalesces damage into one bounding box —
so a paint touching row 1 and row 25 costs a near-full-screen flush even if only two rows
changed. **~40 ms for a full-screen keystroke is about the useful floor**; below that, wins
have to come from touching fewer rows, not from composing faster.

## Where the numbers live

There is no tracked results file. Put the baseline you measure, the per-stage breakdown,
and anything left open into the commit message of the change they justify — a number that
travels with its diff stays true, and one in a side file drifts. Quote the before/after
pair and the tour you ran, so the next pass can A/B against it.
