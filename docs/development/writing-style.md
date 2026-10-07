# How we write

MeshTerm's technical text is written in **ASD-STE100 Simplified Technical English** (STE),
with Canadian spelling. This page gives the rules, the few rules that we bend, and the
glossary. Use it when you write a docstring, a comment, a document, a guide, or a
changelog entry.

STE is a controlled language. It started in the aerospace industry, where a maintenance
manual must have only one meaning for each reader. It has a set of writing rules and a
dictionary of approved words. Its purpose is text that a reader understands quickly and
correctly, also when English is not the first language of that reader.

## What is written in STE

| Written in STE | Not written in STE |
| --- | --- |
| Docstrings and code comments, in `meshterm/`, `tests/`, `scripts/`, and `packaging/` | The licences, `NOTICE`, and other legal text |
| The README, `CONTRIBUTING.md`, `SECURITY.md`, and the documents in `docs/` | The written pages in `meshterm/assets/pages/` (About MeshTerm, About the author, Discord, Support). These are the author's own words |
| The guides, the CLI manual, and the device manuals | `CODE_OF_CONDUCT.md`, which is a standard text |
| The changelog | The one-line description of MeshTerm (the package docstring, `pyproject.toml`, the CLI help). It is fixed text |
| | Quoted text: error messages, log lines, command output, UX strings, and the words of other persons |
| | Identifiers, file names, commands, and code |

UX strings (the text on the screens) follow the UX standards in `CLAUDE.md`.

## The rules

### Words

- Use simple, common words. Use one word for one meaning, and one meaning for one word.
  When the glossary gives a word for a concept, use that word each time.
- Use a word as only one part of speech. "Record" is a noun ("a stored record"). For the
  verb, use "store".
- **Technical names** are permitted: identifiers, file names, commands, product names,
  standards, and the terms in the glossary below. A technical name can end in "-ing"
  ("the setting", "the padding", "a heading", "the encoding").
- **Technical verbs** are permitted: verbs for a computer process, such as compile,
  render, parse, decode, encode, cache, push, pop, flash, boot, install, and log in.
- Do not use Latin abbreviations. Write "for example" for "e.g.", "that is" for "i.e.",
  and "and other ..." for "etc.".
- Do not use contractions. Write "do not", not "don't". Write "it is", not "it's".
- Use Canadian spelling: "colour", "behaviour", "centre", "grey", "licence" (the noun),
  "metre" (the unit), "organize". A "meter" is an instrument (the `meter` widget). Keep
  identifiers and quoted text as they are (`color_map`, the "Apache License").
- Name the actor of an action: "MeshTerm sends", "the function returns", "the screen
  draws". Use "we" only for a decision of the project ("We do not ... because ...").
- The possessive "'s" is permitted ("a handheld's own screen", "M5Stack's Cardputer").
  Use "of" when the possessor is long. Do not make an awkward "of" phrase only to avoid
  "'s" ("the own screen of a handheld" is wrong).

### Verbs

- Use these verb forms: the infinitive ("to start"), the imperative ("Start"), the simple
  present ("starts"), the simple past ("started"), and the future ("will start").
- Use the past participle only as an adjective ("the changed file") or after a form of
  "to be" ("the file is changed").
- Do not use the "-ing" form as a verb. Write "before you start the app", not "before
  starting the app". Write "while the map draws", not "while drawing".
- Use the active voice. In a description, use the passive voice only when the agent is
  unknown or not important.
- Use a verb for an action, not a noun. Write "examine the log", not "do an examination
  of the log".
- Use "must" for an obligation, "can" for a possibility or an ability, and "will" for the
  future.

### Sentences and paragraphs

- An instruction has a maximum of 20 words. A description has a maximum of 25 words.
  An identifier, a number, or a file path counts as one word.
- Write one topic in each sentence.
- Do not omit words to make a sentence shorter. Keep "a", "the", "that", and the verbs.
- Use a vertical list for steps, options, and conditions.
- Use connecting words between related sentences: "then", "but", "because", "if", "when",
  "also", "after", "before", "thus".
- Start a paragraph with the most important information. A paragraph has a maximum of six
  sentences.

### Instructions and safety

- Write an instruction in the imperative: "Run the tests."
- If a condition comes first, put a comma after it: "If the test fails, examine the log."
- Put a safety instruction before the step that has the risk. **WARNING** is for a risk
  of injury. **CAUTION** is for a risk of damage to equipment or a loss of data. Start
  with the instruction, then give the risk.

### Punctuation

- Do not use semicolons. Write two sentences.
- Do not use a dash (`—`) to join two parts of a sentence. Use a period, a comma, a colon,
  or parentheses.
- Use a colon before a list, an example, a quotation, or code.
- Use parentheses only for a short reference, an example, a key name, a unit, or a label.

### Pronouns

- Use "this" and "these" with a noun: "this change", not "this" alone.
- Use "it" only when the noun that "it" refers to is clear and near.

## The rules that we bend

STE is our default, and we do not drift from it. But a rule can bend when the STE form is
less clear or less exact than the normal form. These are the permitted bends:

1. **Modal verbs for uncertainty.** "Should", "may", "might", and "could" are permitted
   when they show that something is not known ("the terminal may not have the font").
   For a known possibility, use "can". For an instruction, use "must" or the imperative.
2. **Perfect and progressive tenses.** "The device has closed the connection" and "a
   flood is in progress" are permitted when they are more exact than the simple tense.
3. **Longer sentences.** A description can be longer than 25 words when a division
   breaks the connection in an argument. A design rationale ("we do X, because Y, and Z
   is the result") is often this type of sentence.
4. **Counterfactual "would".** Code often has a reason in the form "if we did X, Y would
   occur". First, try the simple tense: "If the hint is cut, the cut removes Esc." When
   that form is not clear, "would" is permitted.
5. **Technical verbs and terms of software.** "Check" (code tests a condition), "call",
   "return", "push", "pop", "cache", "parse", "render", and the other terms of the
   glossary are permitted. STE has no dictionary for software, so the glossary is ours.
6. **Phrases where a phrase is normal.** A heading, a table cell, a list item, the first
   line of a docstring, and a short comment can be a phrase instead of a sentence.

Use a bend only when it helps the reader. In all other cases, use the STE rule.

## Docstrings and comments

### Docstrings

- The first line is a summary. It is a sentence or a noun phrase, and it ends with a
  period. A noun phrase is good for a function that returns a thing ("The glyph that a
  command row starts with."). A sentence in the simple present or the imperative is good
  for a function that does a thing ("Clamp the highlight to the list.").
- After the first line, explain what the code does, then why. The reason is the most
  important part of a docstring, because the code cannot show it. Keep each reason.
- Keep the Google sections (`Args:`, `Returns:`, `Raises:`, `Yields:`). Write the
  entries in STE.
- Keep cross-references (`:func:`, `:class:`, `:data:`, `:mod:`). Write "Refer to
  :func:`x`" or "(refer to :func:`x`)" instead of "see".
- Write an identifier in code font: ``like_this``. Keep quoted UX strings exactly, in
  code font or in quotation marks.
- Do not use capitals or italics for emphasis. Write "the only renderer", not "THE
  renderer". Use italics only for a term at its definition. Bold is permitted for a term
  at its definition, and for the one most important fact of a paragraph. Use it rarely.
- Write the history of a decision in the simple past, and keep it short: "The hint once
  had a separate Back row. We removed it, because Esc does the same thing."
- Keep a quotation of a decision exactly, with its name and date ("JP, 2026-09-30: ...").

### Comments

- A comment that explains is written in STE sentences. A short comment can be a phrase
  ("Clamp at both ends.").
- Keep the tool part of a tool comment exactly as it is: `noqa: BLE001`,
  `type: ignore[attr-defined]`, `pragma: no cover`, `fmt: off`, and the SPDX line. The
  explanation after the tool part is prose: write it in STE
  (`# noqa: BLE001 - an optional read. Its absence is acceptable.`).
- Write a quoted UX label in code font or in double quotation marks, never in italics.
- Keep the `#:` mark of an attribute comment. Write its text in STE.
- Keep the layout of a diagram, a table, or an example in a comment. Change only its
  words.
- Do not change commented-out code.

## Glossary

One word for one concept. The words in this glossary are technical names in MeshTerm.
Use them with this meaning only. Use the words in the "Not" column only in identifiers
and in quoted text.

### The mesh

| Word | Meaning | Not |
| --- | --- | --- |
| node | Any device on the mesh: a radio that transmits packets. The types are companion, repeater, room server, and sensor | station |
| contact | A node that your device knows. It is **discovered** (heard, not added) or **added** (in the contact list, and you can send messages to it). Each contact is a node. Not each node is a contact | |
| our node | The node of the companion that MeshTerm is connected to. On the screen, it is "you" (★) | own node, self node, local node |
| companion | A node that a program (such as MeshTerm) controls through USB, Bluetooth, or TCP | |
| device | The companion that MeshTerm is connected to (the `Device` class) | radio (for the companion) |
| handheld | A small computer that runs MeshTerm on its own screen: the PicoCalc, the Cardputer Zero, or the uConsole | device (for the computer), on-device |
| radio | The LoRa radio in a node | |
| Bluetooth adapter | The Bluetooth hardware of the computer that runs MeshTerm | Bluetooth radio |
| heard | Received over the radio from a node ("last heard", "first heard") | seen |
| key | The full value of a public key or a channel secret | |
| hash | The short identifier that comes from a key: the first one to three bytes of a public key (the path hash mode sets the number), a channel hash, or one hop in a path | short key |
| key prefix | The first bytes of a public key, longer than a hash: six bytes (12 hex digits) for the address of a direct message and the id of a node in stored history, or four bytes for the author of a room post. It is a truncated key, never a hash | hash (for a key prefix) |
| path | An ordered list of hops that you write or force (`a1,3d,…`) | |
| route | The sequence of nodes that a trace went through or will go through | |
| hop | One node in a path or a route | |
| via | The prefix for the relay chain of a packet or a message | |
| region | A named area for which a repeater relays floods (`region put yul`). Shown without `#` | |
| scope | The region to which a flood is limited | |
| unscoped | A flood that each repeater relays when it permits the wildcard `*` | |
| flood | A packet that each repeater that hears it transmits again | |
| advert | A packet in which a node broadcasts its name, key, and position | advertisement |
| BLE advertisement | The Bluetooth LE packet in which a peripheral broadcasts its name. It is not a MeshCore advert | BLE advert |
| room | The message board of a room server. You join a room, read it, and post to it | |
| post | A message on the board of a room | |
| packet | One radio transmission | frame (for radio data) |
| protocol frame | One unit of the companion protocol on USB, Bluetooth, or TCP: a command, a reply, or a push from the companion. In code that handles only this protocol, "frame" alone is permitted | packet (for this) |
| message | A text message, direct or on a channel | |
| send | MeshTerm gives a message, a packet, or a command to the device | emit |
| transmit | A radio puts a packet on the air | |
| broadcast | Transmit to all nodes, with no destination | |
| relay | A repeater transmits a packet again | |
| receive | Data from the device arrives at MeshTerm | |

### Settings

| Word | Meaning | Not |
| --- | --- | --- |
| setting | A value of the radio. The device keeps it. You edit it on Device config | |
| preference | A value of MeshTerm's own behaviour, in `preferences.toml`. You edit it on the Preferences page | option (for this) |
| config | The setup of the machine: paths and device profiles, in `config.toml` | configuration file |

### The screen

| Word | Meaning | Not |
| --- | --- | --- |
| user | The person who uses MeshTerm | reader, operator |
| screen | A full-frame place in the app that the user is in: a menu, the map, a list, an editor | view |
| dialog | A floating box that informs, confirms, or asks. When the question is answered, it closes | popup, pop-up |
| prompt | A dialog or a line that asks for typed text | |
| page | All the content of a screen, which can be taller than the terminal. Also a written page | |
| frame | The full image that MeshTerm composes for the terminal in one paint: the title bar, the panel, the body, and the footer | |
| paint | One full update of the terminal | redraw, repaint, refresh (for the terminal) |
| animation step | One image of an animation (a spinner) | frame (for this) |
| panel | The box with a border around the body of a screen | panel (for the hardware) |
| display | The physical screen of a handheld | panel |
| window | An operating-system window: a terminal window, or the window of the emulator. A "time window" is a span of time | |
| terminal | The terminal program or the console that shows MeshTerm | |
| row | One item of a list or a table on the screen. A row can have more than one line | |
| line | One line of text | |
| cell | One character position on the terminal. Widths are in cells | column (for width) |
| column | A vertical part of a table | |
| lane | An aligned column in the rows of a list (`Lane`) | |
| F-key lane | The row of F-key chips at the bottom of a handheld screen | |
| graph lane | The horizontal track of one route in the route graph. In `pathgraph.py`, which has only this meaning, "lane" alone is permitted | |
| marker | The glyph of a node on the map or on the route graph | |
| raster | The rendered picture of the map, before MeshTerm draws it in the frame | frame (for this) |
| legend | The list of node types and their glyphs under a graph or a map | key (for this) |
| chip | A label in reverse video: a button, an F-key label, or one hop of a path line | |
| hint | The line of key hints in the footer, or in the border of a dialog | |
| atom | One item of a hint or a status line, between `·` marks | |
| highlight | The row that `❯` points to | cursor (for the row) |
| pointer | The `❯` glyph that marks the highlight | cursor (for the glyph) |
| screen on the stack | One entry of the navigation stack: a screen or a dialog that is pushed | frame, stack frame (for this) |
| insertion slot | The place in the path composer where the next hop goes | cursor (for this) |
| glyph | A symbol that a font draws | |
| character | One code point in a text | |
| icon | The emoji or glyph at the start of a menu row | |
| mark | A status glyph: `✓`, `✗`, `●`, `○`, `⚠` | |
| colour | Any colour | color (in text) |
| hue | The colour of a node, which comes from its key | |
| light | Show the hash bytes of a key in the hue of its node (`highlighted_hash`) | highlight (for this) |
| style | A named style of the theme | |
| keyboard key | A key on the keyboard. Write the key name when you can ("the Esc key", "F4") | key (alone) |
| setting key, preference key | The name of a setting (`tx`, `radio`) or of a preference in a command or a file. A dictionary key or a JSON key also has its qualifier | key (alone) |
| key press | One press of a keyboard key | keystroke |
| visible | On the screen | in view |
| viewport | The part of a page, or of the map, that the screen shows. A page that is taller than its viewport scrolls | view, window (for this) |
| list window | The visible rows of a list that scrolls inside a screen (`ListWindow`) | window (alone) |

### The program

| Word | Meaning | Not |
| --- | --- | --- |
| call | Code calls a function | invoke |
| caller | The code that calls a function | |
| return | A function gives its result to the caller | hand back, give back |
| run | Start and do a tool, a command, a test, or a process. As a noun, one execution of a command or of MeshTerm | invocation |
| select | The user chooses a row, a value, or an action | pick |
| start, stop | Begin or end a process, a service, or a task | launch, kick off, halt |
| open, close | Begin or end a screen, a dialog, a file, or a connection | |
| leave | The user goes from a screen back to the screen below it. Also, the user leaves a room: MeshTerm forgets it, because a member cannot resign | |
| quit | The user leaves the app | |
| exit | A process ends. Its result is the exit status | |
| draw | Put text or glyphs on the frame | |
| render | Change data into text, ANSI bytes, or a document | |
| show | Make information visible to the user | display (as a verb) |
| print | Write to stdout or stderr | emit |
| read, write | Get bytes from, or put bytes into, a file, a stream, or the device | load, fetch |
| store | Keep data in the database or a file for later use | persist, record (verb) |
| save | Do an explicit save: the user's Save, or the device's save command | |
| record | One stored data item | |
| delete | Remove stored data permanently | purge (except the Purge feature) |
| remove | Take an item out of a collection or a text | drop, strip |
| clear | Make empty | |
| download | Get a file from the network | fetch |
| device file | A file in `/dev` (`/dev/spidev0.0`, `/dev/ttyUSB0`) | node (for a device file) |
| check | Code tests a condition | |
| make sure | Code or a person causes a condition to be true | ensure |
| examine | Look at something carefully | inspect |
| probe | Ask a terminal or hardware what it can do | |
| cache | Keep a result to use it again | memo, memoize |
| refresh | Get a new value for cached data | |
| error | An exception, or a message about a problem | |
| failure | An operation that did not complete | |
| bug | A fault in the code | |

### Words to replace

| Do not use | Use |
| --- | --- |
| ensure, verify, double-check | make sure, check |
| utilize | use |
| perform, carry out | do |
| prior to | before |
| commence, initiate | start |
| terminate, halt | stop |
| obtain, acquire, fetch | get |
| allow, enable, permit | let |
| require, need | is necessary, must |
| inspect, review | examine |
| indicate | show |
| modify, tweak | change |
| eliminate, drop, strip | remove |
| additional | more |
| numerous | many |
| attempt | try |
| determine, figure out | find |
| fix (a fault) | correct, repair |
| since, as (meaning "because") | because |
| rather than | instead of |
| about (a quantity) | approximately |
| regarding, concerning | about |
| in order to | to |
| via (except the relay chain) | through |
| hand back | return |
| in view | visible |
| "e.g.", "i.e.", "etc." | for example, that is, and other ... |
| simply, just, actually, basically, really | (omit) |

## Checks

`tests/test_writing_style.py` searches the docstrings, the comments, and the documents for
the marks that STE does not use: em dashes, semicolons, contractions, and Latin
abbreviations. It does not search text in code font, text in double quotation marks, code
blocks, or URLs.

A document can contain legal text that must stay exactly as it is (a licence notice, a
required attribution). Put `<!-- ste: off -->` before such text and `<!-- ste: on -->`
after it. The test does not search the text between the two markers. Use the markers only
for legal text.

## The source

ASD-STE100 is a specification of the AeroSpace and Defence Industries Association of
Europe (ASD). This page uses Issue 9. The specification itself is available from
[asd-ste100.org](https://www.asd-ste100.org).
