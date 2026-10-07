# UI consistency survey: what is off, and what turned out to be fine

> **A historical record.** This was a snapshot of the code on the date below. We keep it,
> because a project that writes down its own problems is easier to trust than a project
> that does not. We have corrected some of these problems since then, and we have not
> corrected others. Nothing in it is a list of tasks. Read it as "what the code looked like
> then", not as "what is wrong now".

The survey was written on 2026-08-22 against `main` @ `4b29248`. It covers the UX standard
of CLAUDE.md, except navigation. It is a catalogue. It is not a change.

**The short version: the code follows the standard very well.** Six sweeps found nothing at
all. Nine small, specific points remain. This is the useful result. It means that the list
below is almost complete and is not a sample.

---

## Part 1: the clean sweeps

We checked each of these with a program across the whole package. We did not check them by
hand on a sample. We did not keep the scripts. Each script is a dozen lines of `ast.walk`,
and it is easy to write again.

| Rule (CLAUDE.md) | How it was checked | Result |
|---|---|---|
| Footer hint ≤72 cells | `wcswidth` over 56 literal hints | **0 over** |
| `Esc` last in the hint sentence | atom split on `·` | **0 misplaced** |
| No `Enter pick`/`choose`/`commit` | regex over all hints | **0** |
| Filter atom is always `type to filter` | regex over all hints | **0 deviations** |
| No emoji in a screen or dialog title | 86 title literals vs an emoji range | **0** (one hit was a `Choice` row title, where icons belong) |
| Sentence case in titles | capitalised non-leading word, minus proper nouns | **0** (5 hits were markup-wrapped panel headings) |
| Lexicon: "heard" and not "seen", "contacts" and not "nodes list" | regex over all non-docstring literals | **0** |
| Status marks: never `✔ ✖ ✅ ❌` | scan of every source line | **0** (the 3 hits are the fold table that *normalizes* them, and a docstring warning against them) |
| `…` on rows that open a prompt, none on rows that act | 40 row labels via the builders | **1 arguable** (refer to §2.7) |
| Empty state never parenthesised | scan of 53 empty-state-shaped strings | **0** |
| `render.query_line` is the one query line | import graph | **exactly** the four screens CLAUDE.md names: select list, path composer, map, mesh walk |

Two more points are important, because rules of this kind usually decay:

- **`Del` on a row is correctly gated.** The hint atom is visible only when the highlighted
  row can be deleted ([`select.py:448`](../../meshterm/ui/tui/select.py#L448)). This is a
  real example of "never advertise a key that would do nothing". The code implements the
  rule, and the survey did not only assume it.
- **The rule "a chord earns a chip" on the lane of the PicoCalc holds.** The app has one
  bare-letter shortcut, which is `w` in the Time Machine. It has its F3 chip. The reason is
  written in full at
  [`timemachine_screen.py:227-242`](../../meshterm/ui/timemachine_screen.py#L227).

---

## Part 2: the findings

The findings are in order of how visible each one is to a person who uses the app.

### 2.1 Empty-state voice: four sites shout (rule: lowercase, muted, `— explanation`, no full stop)

The house voice is `no contacts yet — receive an advert first`. These four sites do not
follow it. They are the empty states of four complete screens. Thus each one is the *only*
thing on the screen when it shows:

| Site | Current | House voice |
|---|---|---|
| [`chat.py:234`](../../meshterm/ui/chat.py#L234) | `No messages yet — say hello!` | `no messages yet — say hello` |
| [`message_paths_screen.py:199-202`](../../meshterm/ui/message_paths_screen.py#L199) | `Nothing overheard — the radio only logs frames it hears while MeshTerm is listening.` / `No direct-message frames logged in the window.` | lowercase, no full stop |
| [`timemachine_screen.py:335-336`](../../meshterm/ui/timemachine_screen.py#L335) | `Nothing recorded in this window.` + `Press w to widen it.` | lowercase, no full stop |
| [`timemachine_screen.py:453-454`](../../meshterm/ui/timemachine_screen.py#L453) | `Nothing sent in this window.` + `Press w to widen it.` | ditto |

The two `Press w to widen it.` lines raise a second question. They are *hints*, and they are
in the body of a screen where the F-key lane and the footer already advertise `w`. There are
two options. The empty state can include the hint (`nothing recorded in this window — press w to widen it`).
Or we can remove the hint.

These two sites are **not** on this list on purpose. `Never heard`
([`contacts_screen.py:258`](../../meshterm/ui/contacts_screen.py#L258)) is the *row label* of
a purge ladder. `No revisits` ([`records.py:237`](../../meshterm/services/records.py#L237))
is the *title* of a discipline. Both are correctly capitalised.

### 2.2 One dialog inverts the button convention

The rule is: *the safe way out is on the left, and the committing verb is on the right and
is the default*.

This is the Location dialog, at
[`config_editor.py:538-541`](../../meshterm/ui/config_editor.py#L538):

```python
[("Pick on map", "map"), ("Type coordinates", "type"), ("Clear", "clear")],
title="Location",
```

It has no `Cancel` and no `default=`. Thus the leftmost slot, which is for the safe way out,
holds an action. The rightmost slot, which is for the default commit, holds the *destructive*
option (`Clear`). The default is index 0. Esc still cancels, so the user loses nothing. But
this is the only dialog in the app with this shape, out of 49 call sites.

### 2.3 One dialog defaults to the middle button

This is at [`config_editor.py:988-992`](../../meshterm/ui/config_editor.py#L988). The buttons
are `[Cancel, Preview, Apply]` with `default=1`. Thus Enter selects **Preview**, and not the
rightmost button `Apply`.

This is very probably intentional. Before it overwrites the complete config of a device, the
dialog gives the user a preview, which is the careful path. But the standard, as it is
written, says that the rightmost verb is the default. This is the only dialog with three
buttons and a real Cancel. There are two options. The rule can get a clause ("with three
buttons, the *careful* commit is the default"). Or this dialog changes.

### 2.4 Two dialog *shapes* that the standard does not describe

The app has two dialog shapes in addition to the confirm. The app uses both consistently,
but the standard does not describe either:

- **The toggle picker.** It has `[Off, On]`, and the default follows the current value
  ([`config_editor.py:441`](../../meshterm/ui/config_editor.py#L441),
  [`repeater_admin.py:313`](../../meshterm/ui/repeater_admin.py#L313)). It has no Cancel.
  `Esc keep` does that job. This is the footer verb that CLAUDE.md reserves for a value
  picker. It is correct in practice, but the standard does not describe it.
- **The notification with a door to a next screen.** It has `[Trophy case, Close]` with
  `default=1`
  ([`trace_screen.py:637-639`](../../meshterm/ui/trace_screen.py#L637)). It opens without a
  request from the user, after a trace that sets a record. `Close` is the default. Thus an
  Enter that the user pressed by mistake closes the dialog and does not open another screen.
  This is the opposite of the usual rule, and it is correct for a dialog that the user did
  not ask for.

Both designs are good. The gap is that CLAUDE.md describes only the confirm. Thus a future
dialog of one of these two shapes has no example to copy.

### 2.5 Two full screens have no F-key lane of their own, and one of them is Trace

Twelve classes declare `floating = False`. These are full screens and not dialogs. Ten of
them override `fkey_lane`. Two do not override it. They use `Screen.fkey_lane`, which gives
the raw `DEFAULT_LANE` ([`fkeys.py:116`](../../meshterm/ui/tui/fkeys.py#L116)):

| Screen | |
|---|---|
| [`TraceScreen`](../../meshterm/ui/trace_screen.py#L345) (`trace_screen.py:345`) | the busiest screen of the app |
| [`TxSweepScreen`](../../meshterm/ui/tx_screen.py#L93) (`tx_screen.py:93`) | |

`DEFAULT_LANE` is the pager that is *always enabled*. The other ten screens build their lane
from `default_lane(nav=self.content_overflows)`
([`fkeys.py:131`](../../meshterm/ui/tui/fkeys.py#L131)). This dims the chips when the body
fits on the screen. Thus on the PicoCalc, both screens show live `Page ↑` and `Page ↓`
chips, also when there is nothing to page. The standing rule of the lane forbids exactly
this.

Trace needs a proper examination, and not only a one-line change. Its footer
([`trace_screen.py:498-505`](../../meshterm/ui/trace_screen.py#L498)) reads
`↑↓ actions · Enter run · PgUp/PgDn scroll · Esc back`. Each of these keys is a physical key
on the PicoCalc, so no function is *hidden*. But Trace is the screen where F1 to F3 are most
useful. To run again, to flip the path, and to open the composer, the user now needs only
one move of the highlight. Trace is also the only central screen that has not had the lane
pass. We must decide what its three free slots say, and not only gate the pager.

`TxSweepScreen` really is one line: `return default_lane(nav=self.content_overflows)`.

(The About page looks like an exception in a grep, but it is not. It gives `floating=False`
as an argument of the constructor and not as a class attribute. It does define its own lane
at [`about.py:115`](../../meshterm/ui/about.py#L115).)

### 2.6 Dialogs that scroll but declare no lane

CLAUDE.md says that `EMPTY_LANE` is for "a screen with none at all (**every dialog**)". Five
surfaces set it explicitly: the confirms, the text prompt, the busy spinner, the reorder
list, and one trace dialog. But two floating dialogs that really *do* page use
`DEFAULT_LANE` instead:

- `RecordDialog` ([`records_screen.py:146`](../../meshterm/ui/records_screen.py#L146)). It
  shows the story of a record, and it scrolls.
- `PathComposerScreen` ([`path_composer.py`](../../meshterm/ui/path_composer.py)). It is a
  list of hop suggestions, which shows a window of the list.

It is possible that both *need* the pager. If so, "every dialog" in the document is too
strong. We must decide between two rules. The first rule is "dialogs get no lane". Then
these two dialogs need `EMPTY_LANE`, and they lose their paging chips. The second rule is
"a surface gets the pager if and only if it pages". Then the parenthetical text in the
document must say *every dialog that does not scroll*.

### 2.7 One row label that possibly needs an ellipsis

This is at [`config_editor.py:787`](../../meshterm/ui/config_editor.py#L787), and the label
is `Share QR / URI`. The rule is "a row that opens further prompts ends with `…`". This row
opens a *view* (the QR dialog), and it does not open a prompt. Thus the answer depends on the
meaning of "further prompts": is it "each further surface"? Each other row in the app is
clear. This is the only boundary case.

Also note: `Share QR / URI` is the only row label in the app that uses ` / ` as a separator.

### 2.8 `★` has two meanings

CLAUDE.md itself lists it two times: `★ you (yellow)` under node types, and `★ best/winner`
under concept icons. In practice, both are in use. The map and each path line draw `★` for
our node ([`pathline.SELF_GLYPH`](../../meshterm/ui/pathline.py)). The trophy case and
the dialog of a new record draw `★` in the accent colour for a record
([`trace_screen.py:635-636`](../../meshterm/ui/trace_screen.py#L635)).

The two uses are never on the same screen, and the colours are different (yellow and
accent). Thus nothing is unclear today. We note it only because it is the single glyph in
the whole table of marks that has two entries, and the heading "one glyph per concept" says
that this does not occur. If a record must appear next to a path line in the future, this is
where a problem will occur. `🏆` is already reserved for the trophy case, and it can carry
the record.

### 2.9 `node.unknown` and `muted`, not verified

CLAUDE.md is clear that an unidentified node takes `node.unknown`, and that this is
*deliberately not* `muted` ("muted is chrome and may sit a step darker, while an unidentified
node is content you can still act on"). `node.unknown` is used at 11 sites. There are also 26
uses of `style="muted"` in [`pathline.py`](../../meshterm/ui/pathline.py) and
[`widgets.py`](../../meshterm/ui/widgets.py). These are the two modules that draw nodes.

Most of these 26 uses are certainly chrome (separators, labels, and the part of a key that
is not lit, which the standard says must stay muted). But a program cannot decide this rule
in a sweep. A person must look at each use and decide: "this is chrome" or "this is a node
without a name". We list it here as the one corner that we did not audit. It is not a
finding.

---

## Part 3: shortlist

| # | Finding | Sites | Effort | Weight |
|---|---|---|---|---|
| 2.1 | Empty states shout on four screens | 4 (6 strings) | trivial | **high**, because it is the whole screen |
| 2.2 | The Location dialog has no Cancel and its default is an action | 1 | small | medium |
| 2.5 | Trace and TX advertise a dead pager on the PicoCalc. Trace has never had a lane pass | 2 | one line and one design pass | **medium-high** |
| 2.3 | The Restore dialog defaults to the middle button | 1 | a decision, not code | low |
| 2.6 | Two scrolling dialogs inherit the pager that the document says they must not have | 2 | a decision | low |
| 2.4 | Two dialog shapes that the app uses consistently but that no document describes | none | document only | low |
| 2.7 | `Share QR / URI`: an ellipsis or a different separator? | 1 | trivial | low |
| 2.8 | `★` has two entries in a table of "one glyph per concept" | none | document only | low |
| 2.9 | `node.unknown` and `muted` in the two modules that draw nodes | approximately 26 to examine | manual | unknown |

Items 2.1, 2.2, 2.5, and 2.7 are code. The other items are decisions about the standard
itself. For items 2.3, 2.4, and 2.8, the decision can be "the standard must say what the code
already does".
