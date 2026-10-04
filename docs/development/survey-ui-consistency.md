# UI consistency survey — what is off, and what turned out to be fine

> **A historical record.** This was a snapshot of the code on the date below, kept
> because a project that writes down its own problems is easier to trust than one
> that doesn't. Some of what's here has since been fixed and some hasn't; nothing in
> it is a to-do list. Read it as "what this looked like then", not "what is wrong now".

Written 2026-08-22 against `main` @ `4b29248`. It covers the CLAUDE.md UX standard apart
from navigation. A catalogue, not a change.

**The short version: the standard is very well kept.** Six sweeps found nothing at all;
what remains is nine small, specific things. That is the useful result — it means the list
below is close to complete rather than a sample.

---

## Part 1 — the clean sweeps

Each of these was checked mechanically across the whole package, not spot-checked. The scripts
are one-offs and were not kept; each is a dozen lines of `ast.walk` and is trivial to redo.

| Rule (CLAUDE.md) | How it was checked | Result |
|---|---|---|
| Footer hint ≤72 cells | `wcswidth` over 56 literal hints | **0 over** |
| `Esc` last in the hint sentence | atom split on `·` | **0 misplaced** |
| No `Enter pick`/`choose`/`commit` | regex over all hints | **0** |
| Filter atom is always `type to filter` | regex over all hints | **0 deviations** |
| No emoji in a screen or dialog title | 86 title literals vs an emoji range | **0** (one hit was a `Choice` row title, where icons belong) |
| Sentence case in titles | capitalised non-leading word, minus proper nouns | **0** (5 hits were markup-wrapped panel headings) |
| Lexicon: "heard" not "seen"; "contacts" not "nodes list" | regex over all non-docstring literals | **0** |
| Status marks: never `✔ ✖ ✅ ❌` | scan of every source line | **0** (the 3 hits are the fold table that *normalizes* them, and a docstring warning against them) |
| `…` on rows that open a prompt, none on rows that act | 40 row labels via the builders | **1 arguable** — see §2.7 |
| Empty state never parenthesised | scan of 53 empty-state-shaped strings | **0** |
| `render.query_line` is the one query line | import graph | **exactly** the four screens CLAUDE.md names: select list, path composer, map, mesh walk |

Two more worth calling out because they are the kind of rule that usually rots:

- **`Del` on a row is gated correctly.** The hint atom only appears when the highlighted row
  is actually deletable ([`select.py:448`](../../meshterm/ui/tui/select.py#L448)) — a real
  instance of "never advertise a key that would do nothing", implemented rather than assumed.
- **The PicoCalc lane's "a chord earns a chip" rule holds.** The one bare-letter shortcut in
  the app — Time Machine's `w` — has its F3 chip, with the reasoning written out in full at
  [`timemachine_screen.py:227-242`](../../meshterm/ui/timemachine_screen.py#L227).

---

## Part 2 — the findings

Ordered by how visible each is to someone using the app.

### 2.1 Empty-state voice: four sites shout (rule: lowercase, muted, `— explanation`, no full stop)

The house voice is `no contacts yet — receive an advert first`. These four break it, and they
are the empty states of four whole screens, so each is the *only* thing on screen when it shows:

| Site | Current | House voice |
|---|---|---|
| [`chat.py:234`](../../meshterm/ui/chat.py#L234) | `No messages yet — say hello!` | `no messages yet — say hello` |
| [`message_paths_screen.py:199-202`](../../meshterm/ui/message_paths_screen.py#L199) | `Nothing overheard — the radio only logs frames it hears while MeshTerm is listening.` / `No direct-message frames logged in the window.` | lowercase, no full stop |
| [`timemachine_screen.py:335-336`](../../meshterm/ui/timemachine_screen.py#L335) | `Nothing recorded in this window.` + `Press w to widen it.` | lowercase, no full stop |
| [`timemachine_screen.py:453-454`](../../meshterm/ui/timemachine_screen.py#L453) | `Nothing sent in this window.` + `Press w to widen it.` | ditto |

The two `Press w to widen it.` lines are a second question: they are *hints*, sitting in the
body, on a screen whose F-key lane and footer already advertise `w`. Either the empty state
absorbs it (`nothing recorded in this window — press w to widen it`) or it goes.

Deliberately **not** on this list: `Never heard` ([`contacts_screen.py:258`](../../meshterm/ui/contacts_screen.py#L258))
is a purge-ladder *row label*, and `No revisits` ([`records.py:237`](../../meshterm/services/records.py#L237))
is a discipline *title*. Both correctly capitalised.

### 2.2 One dialog inverts the button convention

The rule: *safe way out on the left, committing verb on the right and default*.

[`config_editor.py:538-541`](../../meshterm/ui/config_editor.py#L538) — the Location dialog:

```python
[("Pick on map", "map"), ("Type coordinates", "type"), ("Clear", "clear")],
title="Location",
```

No `Cancel`, no `default=`. So the leftmost slot — reserved for the safe way out — holds an
action, the rightmost — reserved for the default commit — holds the *destructive* option
(`Clear`), and the default falls to index 0. Esc still cancels, so nothing is lost; but it is
the only dialog in the app shaped this way, out of 49 call sites.

### 2.3 One dialog defaults to the middle button

[`config_editor.py:988-992`](../../meshterm/ui/config_editor.py#L988) — `[Cancel, Preview, Apply]`
with `default=1`, so Enter lands on **Preview**, not the rightmost `Apply`.

This is very likely deliberate — previewing before overwriting a whole device config is the
cautious path — but the standard as written says the rightmost verb is the default, and this
is the only three-button dialog with a real Cancel. Either the rule grows a clause ("with
three buttons, the *cautious* commit defaults") or this changes.

### 2.4 Two dialog *shapes* the standard doesn't describe

Beyond confirms, the app has two other dialog shapes, both used consistently but neither
written down:

- **The toggle picker** — `[Off, On]` with the default tracking the current value
  ([`config_editor.py:441`](../../meshterm/ui/config_editor.py#L441),
  [`repeater_admin.py:313`](../../meshterm/ui/repeater_admin.py#L313)). No Cancel; `Esc keep`
  carries it, which is exactly the footer verb CLAUDE.md reserves for a value picker. Correct
  in practice, undescribed in the standard.
- **The notification with an onward door** — `[Trophy case, Close]`, `default=1`
  ([`trace_screen.py:637-639`](../../meshterm/ui/trace_screen.py#L637)). Appears unbidden after a
  record-setting trace. `Close` is the default so a stray Enter dismisses rather than
  navigating — the inverse of the usual rule, and right for a dialog the user did not ask for.

Both are good designs. The gap is that CLAUDE.md describes only the confirm, so a future
dialog of either shape has nothing to copy.

### 2.5 Two full screens have no F-key lane of their own — one of them is Trace

Twelve classes declare `floating = False` (they are full screens, not dialogs). Ten override
`fkey_lane`. Two do not, and so inherit `Screen.fkey_lane` → the raw `DEFAULT_LANE`
([`fkeys.py:116`](../../meshterm/ui/tui/fkeys.py#L116)):

| Screen | |
|---|---|
| [`TraceScreen`](../../meshterm/ui/trace_screen.py#L345) (`trace_screen.py:345`) | the app's busiest screen |
| [`TxSweepScreen`](../../meshterm/ui/tx_screen.py#L93) (`tx_screen.py:93`) | |

`DEFAULT_LANE` is the *always-enabled* pager. The ten others build from
`default_lane(nav=self.content_overflows)` ([`fkeys.py:131`](../../meshterm/ui/tui/fkeys.py#L131)),
which dims the chips when the body already fits. So on PicoCalc both screens advertise live
`Page ↑` / `Page ↓` chips whether or not there is anything to page — the one thing the lane's
standing rule forbids.

Trace is the one worth a proper look rather than a one-liner. Its footer
([`trace_screen.py:498-505`](../../meshterm/ui/trace_screen.py#L498)) reads
`↑↓ actions · Enter run · PgUp/PgDn scroll · Esc back` — every key physical on the PicoCalc, so
nothing is *undiscoverable*. But it is the screen where F1–F3 would earn their keep most
(re-run, flip the path, open the composer are all one cursor move away today), and it is
currently the only central screen that hasn't been through the lane pass. Worth deciding what
its three free slots should say, not just gating the pager.

`TxSweepScreen` genuinely is one line: `return default_lane(nav=self.content_overflows)`.

(The About page reads as an exception in a grep but isn't: it passes `floating=False` as a
constructor argument rather than a class attribute, and it does define its own lane at
[`about.py:115`](../../meshterm/ui/about.py#L115).)

### 2.6 Dialogs that scroll but declare no lane

CLAUDE.md says `EMPTY_LANE` is for "a screen with none at all (**every dialog**)". Five
surfaces set it explicitly — the confirms, the text prompt, the busy spinner, the reorder
list, one trace dialog. But two floating dialogs that genuinely *do* page inherit
`DEFAULT_LANE` instead:

- `RecordDialog` ([`records_screen.py:146`](../../meshterm/ui/records_screen.py#L146)) — a record's
  story, which scrolls
- `PathComposerScreen` ([`path_composer.py`](../../meshterm/ui/path_composer.py)) — a windowed
  list of hop suggestions

Both arguably *want* the pager, which would make the doc's "every dialog" too strong. The
question is whether the rule is "dialogs get no lane" (then these two need `EMPTY_LANE` and
lose paging chips) or "a surface gets the pager iff it pages" (then the doc's parenthetical
should read *every dialog that doesn't scroll*).

### 2.7 One row label that may want an ellipsis

[`config_editor.py:787`](../../meshterm/ui/config_editor.py#L787) — `Share QR / URI`. The rule is
"a row that opens further prompts ends with `…`". This one opens a *view* (the QR popup), not
a prompt — so it turns on whether "further prompts" means "any further surface". Every other
row in the app is unambiguous; this is the only edge.

Also worth a glance while there: `Share QR / URI` is the only row label in the app using ` / `
as a separator.

### 2.8 `★` carries two meanings

CLAUDE.md itself lists it twice: `★ you (yellow)` under node types, and `★ best/winner` under
concept icons. In practice both are live — the map and every path line draw `★` for our own
node ([`pathline.SELF_GLYPH`](../../meshterm/ui/pathline.py)), while the trophy case and the
new-record dialog draw `★` in accent for a record
([`trace_screen.py:635-636`](../../meshterm/ui/trace_screen.py#L635)).

They are never on the same surface, and the colours differ (yellow vs accent), so nothing is
actually ambiguous today. Flagged only because it is the single glyph in the whole marks table
with two entries, and the "one glyph per concept" heading promises otherwise. If a record ever
needs to appear next to a path line, this is where it bites — `🏆` is already reserved for the
trophy case and would carry it.

### 2.9 `node.unknown` vs `muted`, unverified

CLAUDE.md is emphatic that an unidentified node takes `node.unknown` and that this is
*deliberately not* `muted` ("muted is chrome and may sit a step darker, while an unidentified
node is content you can still act on"). `node.unknown` appears at 11 sites. There are also 26
`style="muted"` uses inside [`pathline.py`](../../meshterm/ui/pathline.py) and
[`widgets.py`](../../meshterm/ui/widgets.py) — the two modules that draw nodes.

Most of those 26 are certainly chrome (separators, labels, the un-lit part of a key, which the
standard explicitly says stays muted). But this is the one rule in the standard that a
mechanical sweep cannot settle: it needs a human to look at each and say "that's chrome" or
"that's an unnamed node". Listed here as the one unaudited corner, not as a finding.

---

## Part 3 — shortlist

| # | Finding | Sites | Effort | Weight |
|---|---|---|---|---|
| 2.1 | Empty states shout on four screens | 4 (6 strings) | trivial | **high** — it is the whole screen |
| 2.2 | Location dialog has no Cancel and defaults to an action | 1 | small | medium |
| 2.5 | Trace and TX advertise a dead pager on PicoCalc; Trace has never had a lane pass | 2 | one line + one design pass | **medium-high** |
| 2.3 | Restore dialog defaults to the middle button | 1 | decision, not code | low |
| 2.6 | Two scrolling dialogs inherit the pager the doc says they shouldn't have | 2 | decision | low |
| 2.4 | Two dialog shapes used consistently but undocumented | — | doc only | low |
| 2.7 | `Share QR / URI` — ellipsis? separator? | 1 | trivial | low |
| 2.8 | `★` has two entries in a "one glyph per concept" table | — | doc only | low |
| 2.9 | `node.unknown` vs `muted` in the two node-drawing modules | ~26 to eyeball | manual | unknown |

Items 2.1, 2.2, 2.5 and 2.7 are code. The rest are decisions about the standard itself, and
2.3, 2.4 and 2.8 may well resolve as "the standard should say what the code already does".
