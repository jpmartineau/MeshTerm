# Changelog

This page lists the notable changes to MeshTerm. The newest version is first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). One
rule of SemVer applies to a leading zero: while the major version is `0`, a **minor** bump
can change how things behave. It does not only add new things.

## [Unreleased]

## [0.10.2] - 2026-09-29

**It is harder to leave the app by accident. The dashboard and the Time Machine have a scope
picker. A T-Deck can pair over Bluetooth.** Esc no longer takes you to the quit dialog. ^Q
asks before it quits. Both traffic screens can show one region only. A T-Deck that runs
MeshOS or wadamesh pairs like any other companion.

### Added

- **The dashboard and the Time Machine can show one scope at a time.** Press `s` to step
  through these views: all traffic, unscoped floods, each region heard, and floods of an
  unknown scope. On the PicoCalc, press F3 on the dashboard or F2 on the Time Machine. The
  charts, the counts, and the signal figures show only that scope. The title says which
  scope you look at. A direct packet has no scope, so it counts only under all traffic.
- The Channels list now starts each row with the slot number of the channel.
- The Contacts title now counts archived contacts too. On a narrow screen, the title
  becomes shorter.

### Changed

- **Esc at the main menu no longer opens the quit dialog.** Many users press Esc again and
  again to go back to the menu. The presses that came after they arrived used to ask if
  they wanted to quit. Now Esc stops at the menu. It still clears a typed filter. To leave
  the app, use the Quit row or ^Q.
- **^Q asks before it quits, from any screen.** The ^Q key is next to ^W, which goes back to
  the menu. Before, one wrong key press closed the app, and a capture or a queued message
  was lost. Now ^Q opens the same dialog as the Quit row. Your work continues behind the
  dialog, and Esc takes you back. A second ^Q quits at once. The main menu shows this
  key. The hint line says `^Q quit?`. On the PicoCalc, the title bar says it, F3 is
  **Quit?**, and Shift+F3 is **Quit!** (it quits with no question).
- **The dashboard counts each packet one time.** Before, its traffic list counted an advert
  two times: one time as the device reported it, and one time as the packet that carried it.
  Now it counts the packets that MeshTerm hears on the air. It uses the same names and
  colours as the live feed.
- The splash screen has new art, and its white letters are no longer grey.
- The Patreon link on the Support page is shorter, and its QR code is shorter too.

### Fixed

- **A T-Deck that runs MeshOS or wadamesh pairs over Bluetooth.** These firmwares refuse the
  first message until the device is paired. MeshTerm took the silence to mean that this is
  not a MeshCore companion, so the PIN prompt did not come. Now MeshTerm knows this refusal
  and pairs the same way as with any other companion. On Windows, a wrong PIN no longer
  leaves a half-made pairing. Before, that pairing made the next correct try fail.
- **When you reboot from Device config, the reconnect dialog shows at once.** A board behind
  a USB serial adapter could restart without MeshTerm knowing. MeshTerm then sent commands
  to a board that was in the middle of a restart. Over Bluetooth, the reboot also no longer
  waits for one second or more for a reply that the restarting board never sends.

## [0.10.1] - 2026-09-27

**Three small fixes to the command line.** A wrong `records --width` now gives an error
instead of an empty result. The row of the simulator in `devices` no longer shows a stray
`()`. Two help pages now read like the other help pages.

### Fixed

- **`meshterm records --width` refuses a width that does not exist.** The hash widths are 1,
  2, or 4. Before, any other number gave what looked like an empty result. A typo and an empty
  database gave the same answer. Also, `--width 0` had no effect and listed every width. Now
  a wrong width is a usage error that names the three choices, like `--category`.
- **The row of the simulator in `meshterm --mock devices` no longer ends in an empty `()`.**
  The brackets hold a serial port, and the simulator has no serial port.
- **`meshterm platform --help` and `meshterm specimen --help` read like the other help
  pages.** Before, they printed notes for a person who reads the source code, with the
  markup. They also cut their summaries short in `meshterm --help`. Now each command has one
  plain line.

## [0.10.0] - 2026-09-25

**Regions, a uConsole with no bridge, and Bluetooth that works on Linux.** MeshTerm now
speaks the regions of MeshCore. It reads and edits the regions of a repeater. It keeps the
messages of a channel inside one region. It also tells you which region each heard flood
was sent into. MeshTerm drives the LoRa radio of the uConsole itself. A connection is also
much easier to understand. A companion with a PIN pairs on Linux. When a connection fails,
MeshTerm says which step failed and what to do.

### Added

- **MeshTerm drives the LoRa radio of the uConsole itself, with no bridge service.** A
  radio on the SPI bus of this machine is now a connection, like USB, Bluetooth, or TCP.
  To use it, select **SPI radio** on the device screen, or run `meshterm --spi`. The
  command `meshterm devices` lists it with the other connections. MeshTerm starts the node
  when it connects and stops the node when it quits. Thus the radio is free for other
  programs each time MeshTerm is closed, also after a crash. The node keeps its identity,
  name, radio settings, channels, and contacts in `~/.meshterm/radio/`. The first time that
  the node runs, it takes the identity and the contacts of the bridge. Thus it stays the
  node that everyone already knows. It needs the radio library
  (`pipx inject mesh-term 'openhop-core[hardware]'`). A board that has different wiring than
  the AIO v1 gives its pins in the `spi` table of a profile. The bridge is still there for a
  node that must stay on the mesh while MeshTerm is closed.
  ([#21](https://github.com/jpmartineau/MeshTerm/issues/21))
- **You can read and edit the regions of a repeater.** The node page of a repeater shows the
  regions for which it relays floods. It also shows if it relays unscoped floods. Select
  **Ask which regions it carries** to ask it one time. A repeater answers only a neighbour,
  or a request over a known route. Repeater admin has a new **Regions** page for the whole
  tree. On this page, you can do these tasks:
  - Allow or deny floods for each region, and for unscoped traffic.
  - Set the home scope and the default scope.
  - Add and remove regions.
  - Save the changes.

  Each edit takes effect on the repeater at once. A reboot cancels the edits that you did
  not save. Thus the page counts your unsaved edits, and it asks before you leave with
  unsaved edits. Sometimes the tree is too big for one reply. Then the page says that the
  tree is cut, and it gets the rest from the lists of the repeater. On the command line,
  `meshterm regions NODE` asks the same question.
- **You can keep a channel to one region.** Give a channel a **Send scope** on its page.
  MeshTerm then floods its messages into that region only, and only the repeaters that carry
  the region relay them. The title of the chat names the scope. Press **^R** to send again
  the message that you selected (or your last message) as unscoped. Use this when the region
  does not deliver the message. If the radio cannot send under the scope, it refuses before
  it sends anything. Before, the message could go out everywhere with no warning. On the
  command line, `channels scope INDEX [REGION]` reads or sets the scope. `channels list` has
  a new `SCOPE` column. For one message, `chat send --channel N --scope REGION` changes the
  scope. Use `--scope '*'` to send it unscoped. Scoped sending needs firmware 1.10, and `*`
  needs firmware 1.16.
- **Each flood now shows the region that it was sent into.** The packet viewer has a `scope`
  row, and the live feed has a `SCOPE` lane. The card of a received channel message also
  names its region. If no region that MeshTerm knows matches, it shows the code. The code
  changes to a name when a repeater or a channel gives MeshTerm that name. `monitor` has the
  same `SCOPE` column, and a `scope` field under `--json`. A packet that MeshTerm stored
  before this version has no scope.
- **A message that you sent shows the scope that it used.** Its message paths dialog names
  the scope. If no repeater relayed the message, and no repeater that MeshTerm knows carries
  that region, the dialog says this. This is often the answer to the question "why did nobody
  hear that?"

### Changed

- **MeshTerm sends one automatic advert each week at most, and none unless you ask.** A
  MeshCore companion never sends an advert by itself. Thus the hourly zero-hop advert and
  the daily flood advert, which MeshTerm sent by default, were the only automatic adverts on
  the air. A companion does not need so many, and they used a shared channel. They are now
  removed from Device config, with `config advert-cadence`. A new preference replaces them:
  **Weekly advert**. It is off by default. When it is on, MeshTerm floods one advert after a
  device has gone one week with no flood advert. MeshTerm first hears the mesh. Then it
  waits for **Advert quiet** seconds of silence (5, 15, 30, or 60, with 30 as the default),
  and for a random time of 0 to 5 s more. The random time stops two waiting radios from
  colliding. A line under the setting shows where the week stands. When you switch the
  preference on, it starts the week. It does not send an advert. A flood advert that you
  send by hand starts the week again. Each device has its own week. If the week ends while
  MeshTerm is closed, the advert goes out the next time that the device connects. MeshTerm
  removes the two old cadence preferences from `preferences.toml` the next time that it
  saves the file.
- The packet classes of the live feed have shorter names, in a narrower column that never
  cuts them off. The SNR and RSSI labels now line up with their lanes. On the PicoCalc, the
  class has no icon, to make room.
- The Channels list now shows **LAST** before **MSGS**. It shows a **SCOPE** column only when
  a channel has a scope.

### Fixed

- **Bluetooth on Linux works with a companion that has a PIN.** Before, the PIN did not
  reach the radio. The pairing changed to a ceremony with no PIN, which the firmware
  refuses. Thus a correct PIN was reported as wrong, again and again. Now MeshTerm does the
  pairing itself and answers with the PIN. It works over SSH, and on machines with no
  desktop. A companion that was paired before it had a PIN is now paired again correctly.
  When a companion is not paired and you gave no PIN, MeshTerm now reports this in seconds.
  Before, it took 30 seconds. **Unpair & quit** also works on Linux.
- **A Bluetooth connection on Linux no longer loses its own link.** Sometimes the radio
  needs a few tries to open a link. This is normal on a Raspberry Pi. Before, MeshTerm lost
  the link that finally opened. The companion was still connected, so it stopped sending
  advertisements. The next try could not find it, and MeshTerm reported that it was out of
  range.
- **A connection that fails says which step failed.** Bluetooth now shows the difference
  between these cases:
  - A link that never opened.
  - A saved pairing that is no longer good.
  - A wrong PIN.
  - A companion that refused to pair.
  - A connection that timed out.

  The PIN dialog shows the reason. Before, it called each refusal a wrong PIN. A USB port
  now says if it is missing, if another program uses it (ModemManager, on Linux), or if you
  do not have the right to open it (the `dialout` group). Each message, and what to do about
  it, is in
  [When a companion does not connect](https://github.com/jpmartineau/MeshTerm/blob/main/docs/guide/connecting.md).
- **Clock sync explains a radio that has a clock that is ahead.** Before, it called this
  "malformed". MeshCore firmware never sets its clock back. Thus, when the clock of a radio
  was ahead of the clock of the computer (because of a GPS fix, another app, or drift),
  the radio refused each sync. MeshTerm logged this as "the device rejected the request as
  malformed". Now MeshTerm reads the clock of the radio first. If the radio is a few seconds
  ahead, MeshTerm counts it as in sync. If the radio is more ahead, MeshTerm says by how
  much, and that a reboot of the radio resets the clock.
- **The Channels page no longer loses channels when it connects.** Sometimes the radio
  refused one more command while MeshTerm read the channel slots. MeshTerm took the refusal
  to mean "no such slot". Thus each channel after that slot was missing for the rest of the
  session.
- **The radio of the uConsole hears the whole mesh.** Its SPI node, and the bridge, listened
  for a preamble that was shorter than the preamble that MeshCore sends. Thus the chip gave
  up on most packets before the end. It decoded a packet only when it caught the end by
  chance. Now it expects the preamble that the mesh uses for the spreading factor that is in
  use.
- **MeshTerm stores the default flood scope in the same way as the firmware and the MeshCore
  app.** Before, it stored `#name` where they store `name`. It refused a name of 30
  characters, which the firmware accepts. It also built a wrong protocol frame for a name
  with an accent. Device config now also refuses a name that no repeater can list back. These names
  have spaces, commas, or a private `$` region.
- **The SPI bridge remembers its channels when it restarts.** Before, it kept the channels
  only in memory. Thus the Channels page was empty after each reboot. For this fix and the
  preamble fix, run the setup menu of the bridge again and select **2) Install as a
  service**.

## [0.9.0] - 2026-09-22

**The first public release.** Everything in MeshTerm is new today. Thus this entry does not
list changes. It says what MeshTerm does.

MeshTerm is a program that you run in a terminal to work with a **MeshCore** radio. This is
one of the small LoRa boards that people use to build long-range mesh networks. Such a mesh
needs no internet, no phone signal, and no node in the middle. The radios pass the messages
from one to the next until they arrive. Connect one radio to your computer and start
MeshTerm. It shows you what the mesh does.

### What you can do with it

- **Run it in two ways. It behaves the same in both ways.** Type `meshterm` alone to get a
  full-screen menu that you use with the arrow keys. Or type a command, for example
  `meshterm contacts` or `meshterm info`. MeshTerm prints an answer and exits. This is good
  for a script. Add `--json` to nearly all commands, and the answer comes as data that
  another program can read.

- **It listens all the time, and it stores what it hears.** The radios on a mesh announce
  themselves. When your radio is connected, MeshTerm notes each announcement that it hears.
  It notes which radio sent it, how strong the signal was, and where the radio said that it
  was. MeshTerm stores all of this on your own computer. Thus the lists and the charts can
  show what happens now, and also what happened last Tuesday.

- **You can see where everyone is.** MeshTerm draws a street map in the terminal, with the
  radios on it. You can pan the map, zoom it, and search it. There is also a Time Machine.
  It has charts of everything that you ever heard. You can see when a radio usually talks,
  and how well you heard it. You can see one day, one week, one month, or all of the time.

- **You can talk to people.** MeshTerm has group channels and one-to-one messages, and both
  are live. If a person is out of range now, the courier holds your message. It delivers the
  message when the person comes back. You can share a channel as a QR code that another
  person scans, or as a link.

- **You can measure the mesh. You do not need to guess.** Trace the route of a message, and
  see which hop is the weak one. Sweep your transmit power from coarse to fine, to find the
  lowest setting that still gets through. Walk a map of how the whole mesh is really
  connected. Often, it is not connected in the way that you thought.

- **It talks to your radio in the way that your radio talks.** MeshTerm uses a USB cable,
  Bluetooth, or the network. It finds the radios that it can find by itself, and asks you to
  select one. If a connection drops, MeshTerm connects again by itself. If you have no
  radio yet, `meshterm --mock` runs the whole program against a simulated mesh. It stores
  what it hears in a history database of its own, apart from your real database.

- **It runs on the small machines too.** The PicoCalc has a layout of its own, for its
  screen of 53 columns. It also has its own fonts, colours, and on-screen key labels. The
  uConsole has room for the full desktop layout, and it runs that layout.

- **It works with no internet.** MeshTerm keeps the map tiles on disk after it downloads
  them. When there is no network at all, the map changes to a plain grid. MeshTerm never
  needs to contact a server to work.

- **When something goes wrong, MeshTerm can describe itself.** Run `meshterm diagnostics`,
  or open the Diagnostics page in the menu. It puts everything that a bug report starts
  with in one block. The block shows which build this is and how it was installed. It shows
  the operating system, the terminal and its size, and if the radio answered. It names
  nothing private. It has no keys, no passwords, no positions, and no contacts. It shows
  your mesh as counts, not as names. One key press saves it to a file that you can attach to
  an issue.

### Getting it

You download one file, and you install nothing else. The files are for Windows, macOS
(Intel and Apple silicon), and Linux (also 64-bit ARM, which covers the uConsole). If you
prefer Python, `pipx install` also works, on Python 3.10 to 3.14. The README has the exact
commands.

MeshTerm is free and open source, under the Apache 2.0 licence. The licence does not cover
the name and the logo (refer to `NOTICE`). A fork is welcome under its own name.

[Unreleased]: https://github.com/jpmartineau/MeshTerm/compare/v0.10.2...HEAD
[0.10.2]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.10.2
[0.10.1]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.10.1
[0.10.0]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.10.0
[0.9.0]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.9.0
