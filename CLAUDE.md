# MeshTerm — working notes for Claude

MeshTerm is a full-featured TUI MeshCore client for your terminal. The UI is a
prompt_toolkit session rendering Rich content (`meshterm/ui/`), tools register into a
menu (`meshterm/tools/`), services run in the background (`meshterm/services/`), and
everything heard is recorded to SQLite (`meshterm/persistence/`).

Run the tests with `python -m pytest -q`. Screens must stay readable at each platform's
`readable_cols` — 72 regular, 53 PicoCalc (see **Platforms** below); the dual-platform
gallery test (`tests/test_gallery.py`) is the enforcement point, and every picocalc case
is a hard gate.

## Git workflow

Commit directly on `main` — do not create topic branches unless explicitly asked (this
is a solo repo; branches just create divergence to merge back later). Commit freely, but
never `git push` unless explicitly asked in that moment.

## Licensing — what every file and every build carries

- **Every Python file opens with `# SPDX-License-Identifier: Apache-2.0`** (line 2 after a
  shebang). `tests/test_spdx.py` is the gate. Anything under another licence lives beside
  its own text and says so in its header — the GPL-2.0-only console-font script, the
  MeshCore MIT notice beside the XIAO patch — and never enters the distributed package.
- **The map credits OpenStreetMap in the map's own bottom-right corner**
  (`ui/attribution.py`): the full OpenFreeMap line on arrival, `© OpenStreetMap` after the
  first keystroke, `muted`, on both platforms and in the same words. *Which* corner is the
  frame's, not the drawing's, wherever the frame has one — **regular** sets the credit into
  the panel's **bottom border rule**, right-justified, one rule cell before the corner, the
  way a title sits in the top rule (`Screen.bottom_caption` → `frame._panel_box`), so the
  map's cells are untouched; **picocalc**, whose frame has no bottom rule at all, stamps it
  over the right end of the drawing's last row instead, overprinting what it covers. The
  minimap always stamps. The split is bound once via `platforms.on_platform`, never asked
  per frame. Never a title atom, never a footer character, never a key. The licence and URL
  live on the About page.
- The name and the wordmark are trademarks reserved in `NOTICE`, which Apache §4(d) makes
  every fork carry; `NOTICE` holds only what must travel with a redistribution.

## UX standards

These are binding. Every new screen, dialog, row, or hint follows them; when you touch an
old one that doesn't, bring it along. The enforcement points live in code — build through
them instead of hand-rolling:

- `ui/menus.py` — `exit_rows`, `menu_rows`, `lane_row`, `section_heading`,
  `confirm_discard`, `fit_cells`.
- `ui/markdown.py` — `render_markdown` (THE prose renderer: a page of writing, drawn in
  the language below).
- `ui/widgets.py` — `highlighted_hash` (THE key widget — shows a key, lights its hash),
  `format_ago` (prose ages),
  `format_age` (column ages), `channel_glyph`, `NODE_GLYPHS`, heard-age heat colouring.
- `ui/theme.py` — `name_style`/`node_style` (per-node hues, hash-derived), `snr_style`,
  the `you` white.

### Lexicon — one term per concept

| Term | Meaning |
|---|---|
| node | any device on the mesh — a radio broadcasting packets; roles: companion, repeater, room server, sensor. The umbrella term |
| contact | a node your device knows: **discovered** (heard broadcasting, not yet added) or **added** (in the contact list, messageable). Every contact is a node; not every node is a contact |
| heard | received from ("last heard", "first heard") — never "seen" in UX text |
| key | the full fixed-length value — a node's public key, a channel secret |
| hash | the short derived id — a key's first path-hash-mode bytes (the slice `highlighted_hash` lights), a channel hash, a path hop |
| path | an ordered hop spec you compose or force (`a1,3d,…`) |
| route | the concrete node sequence a trace walked or will walk |
| via | prefix for a packet/message's relay chain |
| Back | leave the current screen/list — Esc's word, and a row's only where leaving is a *choice* (see below) |
| Quit | leave the app (main menu, device splash) — nowhere else |

Node vs contact — the boundary: **node** is the hardware/participant sense — the map,
mesh walk, heard-nodes, relay hops, graph vertices, and node *types* (companion/repeater/room server/
sensor) all speak "node". **Contact** is the saved-identity sense — the Contacts screen,
the courier recipient, anything you *address*. The reception/persistence layer
(`observations.node`, `HeardNode`, `heard_nodes()`, `trace_hops.node`) stays "node"; the
sortable list you pick from is `contactlist.py` (`ContactListScreen`/`ContactRow`/
`ContactsSort`/`contacts_table`). The `contacts` tool lists the device's added contacts.

Setting vs preference — the other boundary, three-way and never blurred: a **setting** is
the *radio's* (`core/device_config.py`, read live from the companion, edited on Device
config); a **preference** is *MeshTerm's own behaviour* (`core/preferences.py`, defaults in
code, overrides in `preferences.toml`, edited on the Preferences page — `ctx.preferences`,
or `preferences.current()` where there is no context to reach through); and **config** is
machine setup — paths and device profiles (`core/config.py`, `config.toml`, a text editor
only). A behaviour value belongs in the preference registry, not as a module constant and
not in `config.toml`: a code constant a reader can't reach is a preference nobody has.

What MeshTerm writes, it writes from what it knows: a store reads its file into typed
records and writes back out of *them*, never the raw document it read, so a field no
record knows (retired, or invented by a hand edit) is gone at the next write — every
`*_store.py` and `preferences.toml` work this way, and the settings store keeps only
keys `device_config` still registers, since it offers them back to the radio. And
**a name that leaves is retired for good**, never reused with another meaning, because a
value can outlive its key in a file nobody saved since, and an old `30` that passes a new
spec means something else undetectably. A preference key goes in `preferences.RETIRED`
(loading refuses it; `report_dropped` logs what the next save will drop); a stored field
goes in its record dataclass's own `RETIRED` set — the record is the path, not the leaf.
`tests/test_retired.py` fails any live name found in either. Units live in the name
(`_s`, `_hours`, `_db`), so a changed unit is a new name by construction.

Relative ages: `format_ago` for prose ("now", "5m ago", "never" — never "now ago"),
`format_age` for aligned columns ("now", "5m").

### Navigation — the stack

Navigation is a **strict stack**: entering a screen or a dialog pushes one frame, Esc pops
exactly one, and the frame you land back on is the one you left — same object, so its
cursor, sort, scroll and typed filter are simply still there. Exceptions are added
deliberately, one at a time, and say why in the code.

- A screen that **owns a loop** is a hub, and a hub *stays pushed* for the whole visit:
  `async with session.stay(screen) as visit:` / `while True: x = await visit.result()`.
  One push, one pop, however many rounds. Never pop-and-re-push a screen to give a dialog
  a backdrop — a screen that never left the stack already is one. `run_screen` remains the
  one-shot form: push, await, pop, for a dialog or a prompt.
- A **sub-view nests**; nothing flattens the stack to spare the reader a climb. Two peers
  may open each other (trace ↔ trophy case) and that cycle is fine — ^W is the climb.
- A list whose **content is data** (an editor's staged values, a queue that lost a row)
  refreshes in place with `SelectScreen.replace_items`, which follows the highlighted row
  by value and keeps the filter. Rebuilding the screen is for when the rows it was holding
  a place in are genuinely gone (a purge, a delete) — and then say so.
- **A popup is not a place.** The stack is for *places* — full-frame screens the reader
  is *in* (a menu, a map, a list that is the tool's own page, an editor). A popup
  *informs, confirms, or asks*, and then it is gone: it is never kept pushed as a hub
  under the screen it was gathering for, so Esc from that screen lands where the tool
  was launched from, not on a picker. The test is whether the reader is in it or being
  asked something — a `SelectScreen` can be either (Channels is a place; "node to
  manage" is a question). A question **draws as a box** even when it is the only frame
  on the stack (the main menu is popped while a tool runs, and a lone floating screen
  would otherwise be painted full-frame): `session.select(floating=True)`,
  `session.text(floating=True)`, `session.stay(screen, dialog=True)` — all through
  `TuiSession._floated`, which puts a blank base under it.
- **An entry chain is a stack too.** A flow that asks two or more things in a row runs
  through `menus.run_steps`: each step gets the answers so far (to label itself, and to
  offer its own previous answer as its default) and returns `None` to step back, so Esc
  undoes one step instead of the whole flow. A trailing Cancel/verb confirm is still a
  decision, not a step — its Cancel abandons. Where the steps are *lists to pick from*,
  the chain is **one popup that turns its page** — `menus.run_wizard` over `WizardPage`s
  (`SelectScreen.turn_page`; title `Feature — subject · step 1 of 2`), never two popups
  stacked. Esc on a later page turns back one, with that page's previous answer
  highlighted; the box is popped before whatever the answers were for opens.
- **Esc peels before it leaves.** A screen carrying a find-as-you-type filter treats the
  typed query as the most recent thing the reader entered: the first Esc clears it and the
  screen stays, the second leaves. One rule on all four (`SelectScreen` and everything
  built on it, the path composer, the map, the mesh walk), and the footer says `Esc clear`
  for as long as it is true. Chat is the same shape with a different peelable thing — its
  first Esc unpicks a selected message. It used to be a 2–2 split, which put opposite
  outcomes behind the same keystroke on the same affordance.
- **^W** unwinds every frame back to the main menu, **^Q** quits from anywhere. Both are
  answered in `TuiSession._dispatch` and neither is ever advertised — a global verb has no
  screen to belong to, and the F-key lane has only three free slots per screen. They are
  stated once, on the About page. `test_navigation` walks every string literal in the
  package to keep them out of the UI. ^W arms **every frame on the stack**, not just the
  top (`request_pop_all`), stopping at anything modal: a screen opened from a key handler
  — the packet viewer, the chat's delivery paths, a trace path flow — runs in a task no
  navigation frame awaits, so an unwind sent only to the top died there while the hub
  below sat on a `visit.result()` nothing would resolve. Such a flow is launched through
  `session.run_detached`, which absorbs the duplicate `PopToMenu` its task cannot carry;
  the stack is what the unwind actually travels down.
- `modal` is *owning the keyboard* (a prompt, progress, a busy splash); `floating` is only
  *drawn as a box*. They are not the same flag: a select list floats and is not modal, the
  busy splash is modal and does not float. ^W declines to unwind past anything modal.
- A caller tests one thing for "the user left": `CANCEL` (or the `None` that `ui.select`
  folds it to). `POP_ALL` never reaches a caller — the navigation boundary turns it into
  `PopToMenu`, which derives from `BaseException` so it crosses the app's `except Exception`
  tool guards; only the menu loop catches it.

### Screens and lists

- **No screen carries an exit row.** Esc leaves — it is on both platforms' keyboards and
  every footer hint says so — so a row that only repeated it was two lines out of every
  screen (a row in thirteen on the PicoCalc's 26) buying nothing. Lists end on their last
  content row; a hand-drawn action list ends on its last verb. The gallery enforces it:
  no rendered line may be the bare word **Back**.
- The exception is where leaving is a **choice** rather than an exit, which is the same
  rule that keeps a dialog's Cancel button. `exit_rows(staged, …)` draws nothing while
  clean and, once changes are staged, one blank `Separator(" ")` line then
  `✓ Apply n staged changes` over `✗ Back — discard staged changes` — Apply has no key of
  its own, so it needs a visible counterpart naming what the other way out costs. Same
  shape in `ReorderScreen`. A **Quit** row is likewise kept (main menu, device splash):
  it *initiates* the app's terminal action behind a confirm, and on the splash it is the
  only statement that the app can be left at all.
- **A cursor clamps at both ends — nothing in the app rolls over.** ↓ off the last row
  and ↑ off the first stay put, in a select list, a hand-drawn action list, a reorder
  list, a dialog's button row, everywhere. The window follows the highlight, so a roll
  hauls the whole list back to the other end and flips its edge markers: the one move
  that reads as the screen changing under you rather than as one step, and held-down
  arrows should settle at an end rather than loop. The exception is a **forward-only key
  with no reverse of its own**, which cycles or it dead-ends — Tab on a button row, and
  the picocalc F-lane's single-chip advancers (`Sort →`, the node page's tab chip, the
  Time Machine's window chip). Those state it in the code.
- Grouped-list section headings use `section_heading("Label")` → `── Label ──` accent.
  That is also what makes a heading *sticky* (it pins to the top row while its section
  scrolls, and the ^PgUp/^PgDn jumps step by it), so build them through it — a hand-rolled
  `Separator` is no landmark. Prose written *directly under* a heading (a description of
  what the section holds) is its preamble and pins with it, in order, as each row scrolls
  off; prose after the section's first row labels nothing and never pins.
- Label + description command rows go through `menu_rows` (two cell-aligned lanes,
  description muted). Editor rows (setting/value/description) go through `lane_row`.
- A row that opens further prompts ends with `…`; a row that acts immediately doesn't.
- Empty states are lowercase muted, optionally `— explanation`, never parenthesized.
- Body section headings inside screens: accent title, optional muted `  ·  note`.
- A find-as-you-type screen echoes its live query as `/query` in `warn`, on its own body
  line **directly above whatever the query narrows** — `render.query_line` is the one
  definition (select list, path composer, map, mesh walk). A screen that *also* carries
  the query in its footer hint draws the body line only where the footer isn't drawn
  (`Platform.footer_fkeys`), never both: the query must be visible on every platform, and
  twice on none.
- **A QR code is white on black, and when it is the answer it is the whole screen.**
  Modules are pure white ink on a pure black field whatever theme the terminal runs
  (`qr._STYLE`, both ends named) — a camera reads contrast, light-on-dark is what a
  scanner expects of a screen — and never black on white. A share screen
  (`qr.share_screen` → `qr.QrScreen`: the channel share, the contact card, the CLI's
  share commands in the menu) is a **bare frame**: `Screen.bare` / `frame.compose_bare`
  draw the body alone on blank rows — no header, no footer, no title bar, no box, no
  hint, no F-key lane, no instruction — with nothing on it but the code and the URL it
  encodes, sat a little above centre; Esc leaves, as everywhere. The code **fits itself
  to the frame every paint**, width and rows both: a lighter error level, then a
  narrower quiet zone, before it would ever be cut (a contact card is 57 cells and 29
  rows at the standard fit; the PicoCalc is 53 across and a regular terminal 24 tall),
  and when code and URL can't share the frame the code is whole at the top and the URL
  a page down. The one exception is a ` ```qr ` fence in a written page, where the code
  is an illustration drawn in the prose where the fence is.

### Written pages

A screen that is *prose* (the three About pages) is **markdown**, not composed rows: the
text lives in `meshterm/assets/pages/*.md` and `ui/markdown.py` draws it in the language
above — `#` the page's own name in brand, `##` a section in the body accent (`## Title ·
note` gives it the muted aside), `###` a sub-heading indented with its prose, paragraphs
and lists hanging as blocks, quotes and fences behind a rail that survives wrapping,
links showing where they go (nothing is clickable on a framebuffer console). Two
paragraphs are page frame rather than body and sit flush and muted: the **standfirst**
under the `#` title and the **colophon**, the last paragraph under a closing `---`. Live
package facts arrive as `{version}` / `{author}` / `{copyright}` placeholders, filled as
the page opens. The `##` headings are the page's landmarks — they pin and the section
jumps step by them — so a written page earns `Sect ↑`/`Sect ↓` on the F-key lane exactly
as a grouped list does. Filling a page in is editing its `.md`; no Python follows.

### Titles

- Sentence case, always ("Trace — Lakeside", "Nodes", "Path width", "Admin login").
- **No emoji in any screen or dialog title** — icons live in menu/list rows. (A terminal
  that draws an emoji narrower than Rich measures leaves a content-sized dialog's border
  short.)
- `—` (em dash) introduces the subject/qualifier: `Feature — subject`. `·` chains status
  atoms: `Map · z12 · 34 nodes · 2.1 km across`.

### Footer hints

- ≤72 cells. Sentence shape: navigation keys, then action keys, **Esc last**.
- Esc verb by surface: `Esc back` leaves a screen · `Esc close` dismisses a read-only
  floating view · `Esc cancel` abandons a prompt/dialog · `Esc keep` leaves a value
  picker unchanged · `Esc quit` only at the main menu · `Esc bye` only on the device
  splash, which is the door rather than a screen — nothing has been started there to
  quit out of. While a find-as-you-type filter is standing the verb becomes `Esc clear`,
  because that is what the press does then.
- A hint that outgrows the surface drawing it **sheds atoms rather than being cut off**,
  because the cut takes the end and the end is Esc (`frame.fit_hint`, the chromeless
  splash). They go from the right, in front of Esc; a screen may name the ones it can
  spare first (`spare_hint_atoms`) — a key the reader would find anyway, like the ←→ its
  move atom already named, before one that is unguessable and advertised nowhere else.
- Enter verb by action: `Enter open` when the row pushes a screen or dialog ·
  `Enter select` when it picks a value or action row · `Enter set` in a value picker.
  A more specific committing verb (`Enter adopt path`, `Enter add`) is fine; a synonym
  of the generic three (`pick`, `choose`, `commit`) is not. The filter atom is always
  `type to filter`. A **button row** is the one place the specific verb is barred: Enter
  commits whichever chip is highlighted, so the hint stays `←→ choose · Enter select ·
  Esc cancel` — `Enter quit` on the quit confirm was true only until ←→ moved, and hid
  the key that moves it. A lone-button acknowledgement names its verb (`Enter OK`) and
  offers no `←→`, having nothing to choose between.
- A hint is drawn **once per frame**, and which surface draws it is the footer's call.
  Where the footer is the hint line (regular) it carries the *top* screen's hint, so a
  floating dialog's border stays silent — the same sentence in the border and at the
  bottom of the terminal was one of them wasted — and its clip arrows fall back to the
  base frame's `↑↓ more`. Where the footer is the F-key lane (picocalc) there is no hint
  line, so the border is the only place Enter/Esc/the arrows can be named and it keeps
  the hint, less every atom whose keys are all chips on the lane one row below
  (`fkeys.strip_lane_atoms` via `frame._dialog_hint`, resolved **per paint** — a screen
  rewrites its hint as its content changes and reads its lane fresh every frame). The
  chromeless splash has no footer row of any kind and keeps its hint in its own border on
  both platforms.
- Key notation: `↑↓ move` (include the verb), `^R`/`^End` for Ctrl chords, `⌫` for
  backspace, `⇧` for shift.
- An **arrow atom** leads the hint and names only the arrows: what they do (`↑↓ move`,
  `←→ choose`, `↑↓ newer/older`) or what they move (`←→ slot`). Several keys share one
  atom only when they do the same thing — `↑↓ PgUp/PgDn scroll`, `Home/End ends`; a key
  that does something *else* gets its own, which is what `←→/Y/N choose` and `↑↓ Tab
  complete` had wrong (Y/N answers, Tab completes, neither is what those arrows do). The
  subject must be the right one, too: *cursor* is the row `❯` points at, so the composer's
  ←→ moves its `slot`. Every button dialog carries the same hint,
  `←→ choose · Enter select · Esc cancel` — a yes/no prompt is a button row like any
  other, so its y/n accelerators ride its `Yes`/`No` labels rather than the line.

### Dialogs

- Modal popups over the pushed backdrop (never full-screen replacements).
- Buttons are reverse-video chips (`  Label  `, `selected` style): safe way out on the
  left, the committing verb on the right **and default**, so Enter commits and Esc backs
  out. Two escalating caution tiers theme the frame: `danger=True` (amber) for a
  disruptive choice — discard edits, reboot, show private key — and `destructive=True`
  (the reserved red) for irreversible **data loss**, so a delete confirm reads red like
  its `typed_confirm` sibling. Bulk or irreversible deletions gate behind `typed_confirm`
  (also red); a single-record delete is a red Cancel/Delete confirm.
- Confirms are Cancel/Verb button dialogs, not Yes/No. Prompt lines that ask for input
  end with a colon.
- Dialog `title` is short; the question/instruction goes in `prompt`.

### Marks and icons — one glyph per concept

- Status marks (single-width, themed): `✓` ok · `✗` err · `⚠`/`?` warn · `●` unread/
  unacked (err) · `○` acked/empty (muted). Never `✔`, `✖`, `✅`, `❌` as status.
- Node types (shared with the map): `★` you (yellow) · `●` companion · `▲` repeater ·
  `■` room server · `◉` sensor · `○` unknown.
- Path cuts: a path line drawn as **chips** that gets cut — a lane that ran out, a row
  scrolled past its edge — breaks the chip off on a half block in that chip's own fill
  (`▐` ends a line, `▌` opens one; `pathline.cut_mark`/`cut_to`), so half the cell is
  segment and half is bare page. An ellipsis would say a *word* was shortened; the crack
  says the segment continues. Arrow-drawn paths keep the `…` — nothing to shear. Distinct
  from `⋯`, which marks whole hops *elided* out of the middle (`PathLine.ellipsized`).
- Path endpoints: a path line runs between the nodes it actually went between — the true
  origin and destination, never the first and last *relay*. Our own end is always the
  app-wide `★` (`pathline.SELF_GLYPH`, taken straight from `marks.SELF_MARK`), never our
  name: the one node the reader never has to be told, and the cells belong to the hops
  that differ from row to row. As a chip it is the map's yellow star on neutral dark
  grey, padded like every other chip.
- Chip seams are **one** interlocked chevron (previous fill on next). Two chips of the
  same fill are the exception the interlock can't draw — and so are two fills the eye
  can't tell apart: under `pathline.SEAM_BLUR` apart in OKLab (`ui/oklab.py`, THE
  perceptual colour distance — never a hue gap, never sRGB, both of which mis-size the
  greens against the cyans). There the seam is the **thin** chevron (`POWERLINE_THIN`)
  in the previous chip's own fill shaded `SEAM_SHADE` darker (lighter for a dark fill),
  drawn on the next, so the ribbon runs on unbroken and the join is a line the chip draws
  on itself, never a wedge of page cut out of the route and never a third colour. An
  elision breaks the ribbon rather than joining it — bare `⋯`
  on the page between a closing point and the next chip's notch, no fill, no padding.
- The ribbon's **outer ends**: the chevron means *the route continues*, so an end that is
  the route's own never wears one. It ends **square** — nothing appended, the last chip's
  own pad is the edge — and opens square the same way; a full Nerd Font rounds such ends
  into a lozenge instead (`POWERLINE_ROUND_*`, drawn only where `termfont.powerline_full`).
  Two things put the chevron back, and either is enough. A *wrapped* line the path outruns
  closes on the point: there the route really does go on. And a line drawing only the
  **middle** of a route — a packet's `via` chain, the TX sweep's composed relays: hops that
  are neither the node the frame came from nor the one it reached — opens on the notch and
  closes on the point, because a square end is the promise that the chip beside it is
  where the route began or ended (`PathLine(from_origin=…, to_destination=…)`, defaulting
  to a whole route). Arrow mode spells the same claim with the separator alone, a leading
  or trailing `→` with no hop on the far side of it.
- Concept icons: 📡 advert · 🕒 clock/sync · 🔄 reboot · 💾 backup · 📂 restore ·
  🔑 channel/credential key · 🔐 identity/auth secret ·
  🔒 lock (a locked contact, a private channel) · 🔓 unlock · 🗑 clear/delete · ✎ compose/edit ·
  ⚙ parameter · `#` count · ▶ run · ⚡ explore/probe · ★ best/winner · ⭐ watch ·
  📤 send now · 📨 courier/queue · 💬 chat · 🔔 notify · 🔕 mute (notifications off) ·
  📱 QR · 🔗 link · ↻ re-read · ↕ reorder · ⇄ reverse (flip a path's direction) ·
  🏆 trophy case/record · ⌨ command line · 📖 read/about · 💰 support/donate ·
  🚪 quit. Packet-class icons (feed/viewer
  lane): 📢 advert · 📊 telemetry · 📦 packet · 💬 message · ✅ ack. Raw payload
  classes (`PAYLOAD_ICONS`): 📻 channel text · 💽 channel data · 📩 direct message
  (overheard) · 📥 request · 📮 response · 🎭 anon request · 🧭 path · 🎯 trace ·
  🧩 multipart · 🧰 control · ❔ unknown — raw advert/ack reuse 📢/✅.

### Colour

- Node names are always coloured: `name_style(name, key)` palette hue, derived from the
  node's key (its first byte — any known prefix agrees) so a rename keeps the colour.
  One rule on **every** platform; only the resolution changes (see **Platforms**).
  A surface holding only a name resolves it first (`make_name_key_resolver`); a node no
  key can place — an unresolved sender, a bare hash standing in as a name, an `○` ring —
  takes `node.unknown`, THE light grey for an unidentified node, everywhere and on both
  platforms. Colour is reserved for keyed identities, never seeded from a name's
  characters. `node.unknown` is deliberately not `muted`: muted is chrome and may sit a
  step darker, while an unidentified node is content you can still act on. Our own node
  is the pure-white `you` style; a context colouring (chart quality) may still win.
- **White is what "you picked this" looks like**, and nothing else in the app is allowed
  to say it. The cursor row — the row `❯` points at, in a select list or in a screen
  drawing its own rows — wears `cursor` (white), never `brand`: the wordmark's teal is the
  app's identity, not a selection, and teal/cyan is itself a node hue, so a cyan-keyed
  node used to vanish into its own highlight. White is outside the node spectrum, so it
  can never collide with an identity. The same white lights the **active sort column's**
  heading and triangle (`contactlist._SORT_ACTIVE`, `widgets._sort_header`) — the same
  claim one axis over — and the path composer's insertion-slot chip, so the row you pick
  and the slot it lands in read as one gesture. It rides *under* the row's spans, so every
  lane keeps the colour it set — an age's heat, an SNR reading, a red badge — with the one
  exception the highlight exists for: **a node's key-derived hue folds to the cursor white
  on the cursor row** (`theme.is_identity_style` → `render._whiten_identities`, applied
  once at the render boundary for any Text whose *base* style is `cursor`), so the
  highlighted row reads as one thing instead of as a name arguing with its own selection.
  Only a keyed hue folds: `node.unknown`'s grey stays grey, because the highlight must not
  claim to know a node we can't place. A **path line is spared** wherever it sits on the
  row — there a hue is not decoration on a name, it is what tells one hop from the next and
  what a route graph's one-byte labels are matched by — so `pathline` stamps its own extent
  (`PATH_INK`, a style that draws nothing) and the fold skips what lies inside it. That is
  what makes the arrow form say what the chip form always said, its fills having never been
  in the fold's vocabulary. Reverse-video chips — a dialog's committing button,
  a running action's Abort, the arrow-mode composer slot, the editor cursor — are the
  `selected` fill, a **grey** block (`muted`'s slate; light grey slot 7 on the VT, the one
  grey a reverse can put behind text there, and the same fill the F-key lane uses). A chip
  marks where a press lands; it is chrome, and it does not get a hue.
  Recency heat colours heard/first-heard ages, never names — seven steps on plain human
  boundaries (`widgets._HEAT_STEPS`), each `heat.*` style named for how old the node it
  colours is: white under 5 minutes, then yellow, light red, brown, red, light grey, and
  one cold grey shared by "over a year" and "never heard". A key lane (via
  `highlighted_hash`) lights its hash in the same key-derived hue; the rest of the key,
  and any key standing in as a name, stays muted. UX text says "key" for the lane and
  "hash" only for the short derived id — never "hash" for a truncated key.
- SNR always through `snr_style`; timelines oldest→now left-to-right, grey baseline = 0,
  drawn via `ui/braillechart` only.

### Layout

- Wrapped labelled rows hang under their value block (two-column grid), never column 0.
- Dialogs anchor slightly above true centre, sized for their populated state.
- Radio traffic: single transmissions or a user-chosen sample count with cooldown pacing
  — never bursts.

### The command line — one answer, two faces

Everything above describes the **menu**, which is read by a person sitting in front of it.
The command line has two readers and it no longer pretends they are the same one: the
**plain face** is for somebody at a prompt who typed a command to find something out, and
the **JSON face** (`--json`) is for a program, often on another machine and often later.

The seam that makes two faces possible is the rule everything else here hangs off:

> **A tool states its answer as data. A renderer turns data into bytes. The CLI boundary
> picks the renderer.**

`exit_code` already worked this way — the tool states it, the boundary turns it into a
process status, no tool calls `sys.exit`. The answer travels the same way now. A tool that
*prints* its answer has given it away: it is rendered and gone by the time anything could
offer it in another format, which is why `--json` reached two commands out of twenty and
stopped, each one growing an `if ctx.json_output:` branch above the rendering that restated
the whole answer in a dialect nobody else could reuse.

- `ui/report.py` — `Listing` (records) and `Facts` (one thing, key by key). **A row holds
  the typed value** (`6.0`, `None`, a `datetime`), never a formatted cell; each `Column`
  carries both projections, a plain `Lane` and a JSON function. A `Facts` block also covers
  the one-scalar answer, through `shape=BARE` (`config get` prints its value alone because
  the caller named the key) and the acknowledgement, through `shape=SILENT`.
- `ui/fields.py` — **the shared shapes, one constructor per concept**: `node`, `channel`,
  `position`, `route`, `spec`, `when`, `instant`, `snr`, `flag`, `free`. A listing that
  mentions a node asks for `fields.node`, so every document speaks the same five-key object
  and every listing names its lanes the same way — by construction, not by review.
  `fields.node` is the one that earns the design: **one machine key expands to as many
  plain columns as the surface has room for**, four in `contacts` and one in a hop table.
- `ui/renderers.py` — `PlainRenderer` and `JsonRenderer`, chosen by `ctx.output`.
  **A third format is a new renderer and nothing else**: no tool is touched, no report
  changes. `ctx.output` is an `OutputFormat`, never a boolean, because the boolean was
  already being asked questions it could not answer.
- `ToolResult.report` is `None` for the menu path and for a feature with no scripted face
  (the map, the dashboard). It is **not** `summary`: that is the run log's record of what an
  invocation did, written to the `runs` table on every execution, and folding a five-hour
  `monitor` capture into it would be the price of one field fewer.
- **`ui/surface.py` is untouched by all of this.** `PlainUi`/`TuiUi` is the *menu-vs-terminal*
  split and stays exactly what it is; a report is **returned**, not shown, so `ctx.ui` never
  grows a `report()` method and `TuiUi` never sees one.
- **Streaming is a second, narrower seam** and deliberately not the general path:
  `monitor` and `chat listen` run until a window closes, so their answer cannot be a value
  handed back at the end. They open `renderers.stream(...)` and emit one typed row per
  record. Two commands use it; anything that can build a whole report builds one.

#### The plain face — for a person at a prompt

It is still a Unix utility's output — one record per line, no colour, no frames, stdout is
the answer, the exit status is half the report — but every rule that existed *only* to make
splitting safe has gone, because splitting is the machine face's job now.
`meshterm/ui/script.py` is the whole vocabulary and carries the reasoning; the rules:

- **Alignment is the delimiter.** A name is bare (`script.name`). The *escaping* under the
  quoting stays and always will (`script._escaped`): a node broadcasts its own name and a
  stranger fills in a message body, and neither may end the record it sits in.
- **A time is an age** — `now`, `5m`, `3h`, `never` (`script.age`, delegating to
  `widgets.format_age` so the two faces cannot drift). An absolute instant survives where
  the instant *is* the fact (`script.stamp`, `fields.instant`): the device clock, an
  appointment set with `--at`, a live capture's own `TIME`, every column under `--absolute`.
  `--absolute` is an override of the `cli_time_format` **preference**, not a switch beside
  it — behaviour belongs in the registry.
- **A route is drawn and a path is typed.** `script.route` joins hops with ` → `; a hop is
  `Name (hash)`, or whichever half is known, never an empty `()`. `script.spec` is the
  comma-joined hex `--path` takes back, and it is the one line on either face that
  round-trips, so nothing creeps into it. **Our own node is a hop like any other** — the
  menu's `★` says "you already know who this is", which is true of the reader and false of
  whoever opens the file afterwards.
- **`unknown` is a word and `-` is an absence.** `script.NONE` is the one token for absent;
  `never` is a *value* (a node not yet heard is a fact). A node type stays the word
  `repeater` — a monochrome `▲` would need a legend and the CLI has no legends.
- **A live stream pins its lanes** (`script.stream`), because a capture cannot measure
  columns it has not seen. A value wider than its lane overruns and pushes the row right;
  nothing is elided, and the one unbounded field goes **last** so it can push nothing.
- **A value that round-trips is untouchable.** `config show` prints what `config set` takes
  — an enum's number, `""` for an empty string — and gains nothing cosmetic. `fields.rendered`
  carries the typed value beside the text its own spec produced, so neither face parses the
  other's output.
- **No wrapping, with one exception**: the four written pages (`about`, `about-author`,
  `discord`, `support`) draw on a `script.PAGE_WIDTH` console. The no-wrap rule exists so a
  *record* is never split with its fields under the wrong headings; a paragraph has no
  fields, and unwrapped it is a 600-cell line no terminal can read. **Never wrap a listing.**
- **stdout is the answer; everything else is stderr** — errors (`meshterm: what went
  wrong`), progress bars, log records, `ui.ack`, and a `ToolResult.message`. The last two
  used to be dropped; stderr keeps the promise the dropping was made to keep (a redirect
  catches only the answer) while giving the person at the prompt back their ✓ and their
  count. `script.stderr_console()` stops wrapping off a terminal, because a sentence folded
  at 80 columns is a sentence `grep` cannot find.

#### The JSON face — the machine contract

`--json` prints **the answer itself**, and the report a caller needs is still `$?`.

- **No envelope.** An array for a listing, an object for a set of facts, so
  `contacts --json | jq '.[].node.name'` reads what it looks like. A multi-block report is
  one object: each `Listing` under its own key, each `Facts` merged at the top level.
- **One compact line, `\n`-terminated**, UTF-8 with no ASCII escaping, keys in the order the
  report declares them. A stream is one document per record, **all the same shape** — no
  discriminator, because every reader would pay for it and only a stream would use it.
- **Absent is `null`, never an omitted key**, and typed values throughout: `9` not `"9"`,
  `false` not `"false"`, a body raw and unescaped. `unknown` becomes `null`; a consumer
  already has one spelling for "nothing here".
- **Timestamps are UTC, RFC 3339, `Z`, to the second** — twenty characters, so string
  comparison is time comparison. `--absolute` does not touch this: a local offset is a fact
  about the machine that ran the command, not about the event.
- **A numeric field carries its unit in its key** (`snr_db`, `uptime_s`, `rtt_ms`), never in
  its value, and a key/hash is lowercase hex. **A key is never truncated** — a truncated key
  cannot go back into `--to` or `--path`, so a short id is a `hash`.
- **`--json` changes the rendering, never the report.** Same exit status, same records. An
  empty result prints its empty document (`[]`, or the object with its `null`s) **and still
  exits 5**; a failure prints **nothing on stdout** and keeps its sentence on stderr.
- **"Prints nothing" is never a legitimate JSON answer.** A command whose plain face is
  silent (`config advert`, `channels join`, `courier cancel`) still emits what it did.
- **A command with no data face refuses, loudly.** `--json specimen` is a usage error (exit
  2): its output *is* the colour, and a document of it would be a lie or an empty gesture.
  So is `--json` with no subcommand. The map, the dashboard, the live feed, the watchtower
  and the mesh walk have no subcommand to refuse from, and that stays the honest answer
  rather than a degraded one.

#### The exit status is the report

`core/exitcodes.py`, and the `--help` epilog: 0 ok · 1 failure · 2 usage · 3 no device ·
4 device failed · 5 nothing to report. A tool that ran fine and found nothing returns
`NO_RESULT`; a failure is *raised*, never returned. Two classifications worth restating
because both were wrong somewhere: **nothing transmitted is not a device failure** (a bad
argument is exit 2), and **a rendering flag never moves the status**.

`tests/test_report.py` is the enforcement point for the seam, `tests/test_script_output.py`
for the plain vocabulary, and `tests/test_cli_contract.py` for the commands — including a
derived sweep that runs **every** registered command twice, once plain and once under
`--json`, and pins that the two agree about the exit status.

### Platforms

One codebase, two flavours: **regular** (desktop/ssh, 72 cols, truecolor, emoji) and
**picocalc** (the PicoCalc's 53×26/53×40 framebuffer console, 16 palette slots, a
512-glyph font, no emoji). A frozen `Platform` spec (`meshterm/platforms.py`) resolves
once at boot; consumers bind at platform-switch time via `platforms.on_platform` — never
branch on the platform per frame, and never `from meshterm.platforms import PLATFORM`.

- The borderless title bar (`frame._title_bar`) is this platform's whole panel border:
  `↑↓ ──── Title ─────── Esc back`. The **clip arrows lead the row and are always both
  drawn** — colour reads the scroll (the border's `accent` where that direction has more,
  `muted` where it doesn't), the same live/dim language the lane draws one row below. A
  pair that appeared and vanished, half of it a blank cell, asked the reader to compare
  the row against a memory of itself; fixed furniture means the bar never changes width
  as the body scrolls either.
- **Never emit a raw emoji or bare hex colour into picocalc output.** Icons go through
  `theme.glyph()` (the compact map); everything else is caught by the render-boundary
  fold (`theme.fold_text`, applied in `tui/render.render_to_ansi` and
  `MapCanvas.to_ansi_lines`) — but the fold is the safety net, not the design.
- The glyph contract is `ui/fontset.py` — the installed console font's exact codepoint
  inventory, device-verified. A character outside it is a test failure, not a tofu box
  found on-device. The font itself is built by `scripts/picocalc/calculinux-console-font-6x12.sh`;
  the two files move in the same commit.
- The 16-slot palette is `theme._VT_SLOTS` (programmed via `/etc/vtrgb`; same script).
  `MESH_THEME_16` speaks `color(0..15)` only; backgrounds stop at slot 7. Both themes
  define identical style names.
- **Bold is brightness on the VT**: `bold` on a 0–7 foreground *is* slot N+8, so a
  dim-slot style must state its intent — `not bold` (keep the declared colour) or `bold`
  (the promotion is the point, only `title.muted`), never silent. Rich merges a row's
  base style into every span, so a silent one changes colour inside a selected row. Same
  trap outside the theme: `MapCanvas` drops emphasis entirely where bold is brightness,
  since its colours arrive quantized and it can't know which bank they landed in, and the
  fold's quantizer (`theme._nearest_slot_params`) states intent for it — a dim slot leaves as
  `22;3N`, never a bare `3N`, because `9N` is *how* the console spells bright and adjacent
  art spans (the wordmark's bevels, a raster's neighbouring cells) reset nothing between
  them, so a bare one inherits the intensity bit and lands a bank too high mid-row.
- On picocalc, the node hue and the heat gradient **quantize** — same rule, coarser
  resolution: `node_style` snaps the key's hue to its sixth of the wheel
  (`theme._NODE_SLOT_HEXES`, the six chromatic bright slots) and heat to the `heat.*`
  steps. Nothing loses its colour for being on the console. Marks whose hue is *fixed*
  rather than derived go through a theme name so the slot is chosen deliberately (the
  node types' `type.*`; a raster resolves the same entry via `theme.mark_rgb`) — a naive
  downsample greys the repeater's violet. Footer hints are replaced by the **F-key lane**
  (`ui/tui/fkeys.py`): five `FPair` slots per screen, F1–F5 primary and F6–F10 each
  slot's *opposite number* (physical Shift+F1..F5), labels ≤6 cells. `DEFAULT_LANE` claims
  only **F4/F5 — the pager**, which has no physical key at all; **the jump to either end
  rides the Shift half of the very pager heading for it** (F10 Top behind F5 Page ↑, F9
  Bottom behind F4 Page ↓), because Home and End *are* on this keyboard and only ever
  wanted a chip for consistency. That leaves **F1–F3 free on every screen** for its own
  verbs, and `EMPTY_LANE` for a screen with none at all (every dialog). **A chip names an
  action, never a key** — `Page ↑`, not `PgUp`; `Latest` on a transcript; `Region` on the
  map, whose Home reframes and whose paging zooms. **A directional pair rises toward its
  outer key**: where two adjacent chips are opposite ends of one axis, the *up · in ·
  more* end takes the slot nearer the lane's edge — F5 on the right-hand pair (F4/F5's
  paging, the map's zoom: `Zoom -` then `Zoom +`, a rocker), F1 on a left-hand pair (a
  select list's `Sect ↑` then `Sect ↓`) — and a slot's Shift companion follows its own
  slot's direction. The lane *is* the footer here, so it obeys the hint line's rule
  — never advertise a key that would do nothing. Empty and dim are different claims: leave
  the slot **empty** when the action isn't a thing on this screen (Retry in a channel), and
  clear `enabled`/`opp_enabled` to draw it **dim** (label kept, fill dropped) when it's a
  thing that just isn't available this paint. Dimming is presentational — `handle` stays
  the authority and no-ops. The lane is also the *only* affordance advertisement here, so
  anything the desktop reaches by a chord or a bare letter (a list's `^PgUp/^PgDn` section
  jumps, the Time Machine's `w`, the map's `^U`, a row's `Del`) earns a slot — otherwise
  it is undiscoverable on the device.
- `meshterm specimen` prints the whole visual language through the real funnels — the
  acceptance card on-device, a preview under `--platform picocalc` on the desktop.
- Dev loop: `meshterm --mock --platform picocalc` in a 53×40 window; the gallery and
  `tests/test_theme16.py` carry the contracts.
