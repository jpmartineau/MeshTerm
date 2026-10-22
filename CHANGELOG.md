# Changelog

This page lists the notable changes to MeshTerm. The newest version is first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). One
rule of SemVer applies to a leading zero: while the major version is `0`, a **minor** bump
can change how things behave. It does not only add new things.

## [Unreleased]

## [0.11.5] - 2026-10-22

**The Courier outbox is a table, and Enter opens the message of an entry.** Each section of
the outbox has column headers, and each row shows its full message. A table that is wider
than the screen pans with ←→, and does not cut its rows. A footer hint that is too wide
keeps its Esc.

### Changed

- **The Courier outbox is a table with column headers.** A waiting entry shows TARGET,
  TRIGGER, and MESSAGE. A finished entry shows TARGET, OUTCOME, and MESSAGE. Nothing is
  shortened to fit. When the table is wider than the screen, ←→ pan the rows and their
  headers together, and the footer says so. The column headers stay at the top with their
  section heading while the section scrolls. Typing still narrows the outbox.
- **Enter on a Courier entry opens its message.** On a waiting entry, Enter opens the
  message so that you can edit it. The courier holds the entry while the prompt is open, so
  the old text cannot go out while you edit. On a finished entry, Enter shows the message.
  The menu of an entry is gone: ^R sends a waiting entry now, and Del deletes an entry after
  a red confirm. On the PicoCalc and the Cardputer Zero, `Send` is on F3, with `Delete`
  behind it on Shift+F3.
- The message field of the Courier has the colours of the chat's compose line: a white
  prompt mark and white text.

### Fixed

- **A footer hint that is too wide keeps its Esc.** The footer line cut a hint that was too
  wide at the right edge, and Esc, the last atom of each hint, went with the cut. Now the
  footer removes atoms in front of Esc, as the splash already did.
- **A cut in the text next to a chip ends on an ellipsis.** When a row had a chip and then
  text, and the text ran past the screen, the cut drew a half block in the colour of the
  chip at the right edge. Now the half block is only for a cut inside a path, and a cut in
  plain text ends on `…`.

## [0.11.4] - 2026-10-20

**You can lock a contact from more places, and a serial radio that stops when your laptop
wakes from sleep opens the reconnect dialog.** In the archive preview, ⇧Del keeps a contact
out of each sweep that follows. In a conversation and on the Contacts list, ^K locks a
contact or unlocks it, and a locked sender has a padlock on its chip. The battery gauge of
the Cardputer Zero stays on with the Cap.

### Added

- **In the archive preview, Shift+Delete keeps a contact out of the sweep and locks it.**
  Delete keeps a contact out of this sweep only. ⇧Del also locks the contact, so that no
  sweep that follows can take it. MeshTerm writes the lock when you select Apply, with the
  archive. Esc and Back ask before they discard a lock that is not written yet. If you lock
  each contact in the preview, Apply only writes the locks. On the PicoCalc and the
  Cardputer Zero, `Lock` is on Shift+F3, behind `Delete`.
- **^K locks the author of a message in a channel, a direct chat, and a room.** Select a
  message and press ^K. Press it again to unlock the author. The status line tells you what
  the key did. On the PicoCalc and the Cardputer Zero, `Lock` is on Shift+F2 in a channel
  and in a direct chat. In a room, Shift+F2 is `Log in`, so there the page of the author
  (Shift+F3) has `Lock contact` or `Unlock contact`.
- **^K on the Contacts list locks the highlighted contact or unlocks it.** The padlock of
  the row changes at once. On the handhelds, `Lock` is on Shift+F2.
- **A locked sender has a padlock on its chip in a chat.** The chip reads `🔒 Name`
  (`⚿ Name` on the PicoCalc), in the place where our own chip has its `★`.

### Changed

- **The selected message of a chat makes its chips white.** The text of the selected
  message was already white. Now the mention chips in it, and the chip of its author, have
  a white label too. Each chip keeps its fill, so you can still tell the nodes apart. A
  mention of an unknown node is white on its grey fill.
- In the archive preview, the footer says `Enter open` on a contact, because Enter opens
  its node page.

### Fixed

- **A serial radio that stops when your laptop wakes from sleep opens the reconnect
  dialog.** When a laptop wakes, the kernel can reset the USB device and close the open
  port, but keep the same device file. MeshTerm still saw the port in the list of ports, so
  it thought that the link was up. No reconnect dialog opened, and each command failed with
  `no_event_received` (issue #32). Now the check also reads the connection flag of the
  client, as the Bluetooth and TCP checks already did.
- **The battery gauge of the Cardputer Zero stays on with the Cap.** On battery with the
  Cap, most reads of the gauge failed, sometimes for almost two minutes. The gauge read
  three times in 11 seconds and then waited 20 seconds, so it missed the good periods.
  After seven failed reads, the gauge went away. Now MeshTerm tries again 5.5 seconds after
  a failed read, and the last good reading stays for 10 minutes. A change between charging
  and discharging must be read two times, because a bad read said "Charging" while the
  battery discharged.

## [0.11.3] - 2026-10-18

**No log keeps a password or a secret, and a bug report can hide your home folder and the
address of your radio.** Passwords, channel secrets, and pairing PINs went into the log and
the run log as plain text, and this release removes them, also from the old records. The new
**Anonymize** preference masks your personal details in the log and in the diagnostics. The
demo mesh now looks more like a real one.

### Added

- **The Anonymize preference masks your personal details in the log and in the
  diagnostics.** It is in the Diagnostics group of the Preferences page, and it is off by
  default. When it is on, MeshTerm masks the account name in your home folder, the
  Bluetooth address of your radio, IP addresses, and the name of a network companion. Each
  masked value keeps a few characters, in the way that a card number prints as
  `51xx xxxx xxxx 4571`: `C:\Users\j*********u`, `DA:xx:xx:xx:7C:7C`, `192.xxx.x.180`, and
  `m******e.local`. Thus a maintainer can still tell two radios apart. The log masks each
  line as MeshTerm writes it, and a change applies at once. A line that MeshTerm wrote
  before you turned the preference on stays as it is.
- **The diagnostics can be anonymized for one report.** `meshterm diagnostics --anonymize`
  masks the report, and `--no-anonymize` shows it in full when the preference is on. On the
  Diagnostics page, press `a` (Shift+F3 on the PicoCalc) to change between the two forms,
  and `s` saves the form that you see. A new fact, `anonymized`, says which form a report
  is.

### Changed

- **The demo mesh uses the regions of the real mesh.** The regions are `na`, `can`,
  `onqc`, `qc`, and `yul`, each one inside the one before it. The `#montreal` channel sends
  in `yul`. The invented regions are gone.
- **In the demo, messages arrive as they do on a real mesh.** Most messages arrive two
  times, through the two repeaters that our node hears, and most paths use three bytes for
  each hop. A node that our node mentions in a channel answers a few seconds later. TX
  optimize now has something to find: the power of a repeater changes how well the next
  node hears it.

### Fixed

- **The log and the run log no longer keep passwords or secrets.** At the DEBUG level, the
  log file showed the parameters of each command as you typed them, and the run log in the
  database kept them at each level. Thus they held room and admin passwords, channel
  secrets, channel links, and the pairing PIN of `config set device_pin`. Each chat run also
  stored the secret of its channel. Now MeshTerm writes `<secret>` in the place of each
  one, in the log file, on the console, and in the run log, always. The rule finds a secret
  by its name, by its form (a remote `password` command, or `secret=` in a link), and by
  its value, when MeshTerm has read or sent that secret before. The first start of this
  version removes the secrets that earlier versions stored in the run log.
- **The remote command line no longer stores a new password in its history.** The history
  keeps `password` and `set guest.password` without the value, so ↑ still recalls the
  start of the command.
- An error about the device PIN no longer repeats the PIN that you typed.

## [0.11.2] - 2026-10-16

**In a chat, your messages show your name, MeshTerm has a demo mesh, and the colours are
calmer.** Your messages and each mention of you are now easy to find in a chat.
`meshterm --demo` runs a whole mesh over Montréal with no radio. The greys have no blue in
them, the borders are grey, and the node marks are white.

### Added

- **`meshterm --demo` runs MeshTerm in a demo mesh over Montréal.** No radio is
  necessary. The demo has approximately forty invented nodes: repeaters on the high points
  of the city, room servers, sensors, and the people of the mesh. It has a week of traffic,
  conversations on the channels, in a room, and in direct messages, and traces with their
  records in the trophy case. Each run starts from the same state, in a directory of its
  own, so your history never gets a node of the demo. The screenshots in the README and on
  the website come from it.

### Changed

- **In a chat, your messages show your name.** A channel, a direct chat, and a room put
  your messages under `★` and your node's name, and the Messages list shows them in the
  same way. Your name is white, on the same colour of box that your key gives each chip,
  where each other sender has the colour of its key as the text. A mention of your name
  has the same white text, and the message that has it gets a white `★` at the left of
  its time. Thus the messages that name you are easy to find down the left side of the
  chat. Before, your messages showed only the `★`.
- **Your node's star is white.** The `★` that marks your node on the map, in routes, in
  lists, and in a chat is white, the same white as your name in the lists. Before, it was
  yellow. A star in a faded part of a route stays grey.
- **The greys are neutral, and the borders are grey.** Each grey of MeshTerm is now a
  neutral grey, with no blue in it, at the same lightness as before. A plain border is
  grey, where it was indigo. The amber and red borders of a caution keep their colours.
- **Each node mark is white.** The marks of a companion, a repeater, a room server, and a
  sensor (`●`, `▲`, `■`, `◉`) are white, because the shape already shows the type. Before,
  they had four colours. The mark of an unknown node (`○`) stays grey.
- A section heading has the colour of the title of its frame, and it has no icon.
- **In a chat, a name that MeshTerm cannot match to a node is black on grey.** The box
  around a sender or a mention with no known node has black text on a grey box. Thus it
  does not stand out among the coloured names. Before, it was light blue on slate.
- **A mention has no `@` in front of it.** The coloured box around the name already says
  that it is a mention. On a terminal that cannot draw the boxes, a mention keeps its `@`.
- **The line where you write a message shows each mention as a box.** When you type the
  `]` of `@[Name]`, or reply to a message, the mention changes into the same coloured box
  that the chat shows after you send it. The arrow keys move over the box in one step, and
  one Backspace removes all of it. A reply now puts its mention where the cursor is, so you
  can name more than one person in one message. The text that you type is white.
- **On the uConsole and the Cardputer Zero, an imported seed stays a seed.** MeshTerm saves
  a 32-byte seed in the node's `identity.key` as you give it. This is the same form that
  the node uses when it makes its own key. A firmware companion still gets the full 64-byte
  key, because it accepts only that form.
- **A blank line comes before each section.** In each list that has sections (the main
  menu, the Messages list, the Preferences page, Device config, and the others), the line
  comes before each section heading. In a chat, it comes after the messages of each author
  and before each new day. Thus the parts of a screen are easier to find, and a
  conversation is easier to follow when many people write. By default, the line shows on a
  terminal of 24 rows or more. A smaller terminal, and the 14 rows of the Cardputer Zero,
  keep each row for content. The new **Section spacing** preference in the Display group
  sets the smallest terminal that gets the line: any height, 20, 24, 30, or 40 rows, or
  never.
- The About page has a section on the Cardputer Zero. It says that MeshTerm runs on the
  Cardputer Zero and on the Cardputer Zero Lite.

## [0.11.1] - 2026-10-13

**Show private key shows the key again, and the uConsole and the Cardputer Zero can import
an identity key.** On each device, the menu showed the warning of Show private key and no
key. On the handhelds that MeshTerm drives over SPI, an import of a key failed. An import
now also reads a file and accepts a 32-byte seed. MeshTerm no longer keeps the private key
in its history of commands.

### Added

- **You can import an identity key from a file.** On Device config, **Identity key…** has
  a new row, **Import from a file…**. On the command line, `config import-key --from PATH`
  does the same. The file can hold the key as hex text, as `export-key --out` writes it, or
  as raw bytes, as the `identity.key` of a node keeps it.
- **An import accepts a 32-byte seed.** The node that MeshTerm runs on an SPI radio keeps
  its key as a seed. A firmware companion accepts only the full 64-byte key. Thus MeshTerm
  makes the full key from the seed, in the same way as the firmware, before it sends it.
  A key of the wrong length now stops at the prompt, before you confirm the import.

### Fixed

- **Show private key shows the key again.** On Device config, **Identity key…** → **Show
  private key** showed its warning and no key. This occurred on each device since 0.10.0.
  On the command line, `meshterm config export-key` always printed the key.
  ([#26](https://github.com/jpmartineau/MeshTerm/issues/26))
- **You can import a key on the uConsole and on the Cardputer Zero.** On these handhelds,
  MeshTerm runs the node itself, and that node refused each import. Now MeshTerm writes
  the key into the node's `identity.key`, and then restarts the node to use it.
  ([#26](https://github.com/jpmartineau/MeshTerm/issues/26))
- **A firmware that does not accept an imported key says so.** Some firmware builds turn
  the import off. MeshTerm waited until the command timed out, and then showed a refusal
  with no cause.
- **The private key no longer stays in MeshTerm's history.** MeshTerm stores the arguments
  of each command that it runs. The key that you gave to `config import-key` was stored
  there as plain text. Now MeshTerm stores `<secret>` in its place, and when this version
  opens your database for the first time, it removes the keys that earlier versions
  stored.
- **Only you can read an exported key file.** `config export-key --out` and **Export to a
  file…** now write the file with mode 0600, as MeshTerm writes each other file that holds
  a secret. Before, the other users of the computer could read it.

## [0.11.0] - 2026-10-12

**MeshTerm supports the Cardputer Zero, and from a message you can open its author on the
map or on a node page.** Each release now has a package for the Cardputer Zero, and the
release sends it to M5Stack's app store. In a chat or on the board of a room, ^L opens the
map on the author of a message, and ^O opens the page of that node. A name that two nodes
use at the same time is now grey, because MeshTerm cannot tell which node wrote the
message. On the handhelds, a channel, a direct chat, and a room have the same F-key chips.

### Added

- **MeshTerm supports the Cardputer Zero.** Each release has a Debian package for the
  handheld, `meshterm_<version>-1_arm64.deb`, and the release also sends it to M5Stack's
  app store. The package has all that MeshTerm needs, which includes the radio library for
  the Cap LoRa-1262 and the font of the display. Install it from the store on the
  handheld, or from the release page. The store shows a new app after M5Stack examines
  it. The [Cardputer Zero
  page](https://github.com/jpmartineau/MeshTerm/blob/main/docs/devices/cardputer-zero.md)
  has the details.
- **^L shows a node on the map from a chat and from Contacts.** In a chat or on the board
  of a room, select a message and press ^L. The map opens on the node that wrote it. On
  the Contacts page, ^L opens the map on the highlighted contact. The key works only when
  MeshTerm knows where the node is, and the hint at the bottom of the screen names it only
  then. On the handhelds, the chip is `Map`, on F3.
- **^O opens the page of the node that wrote a message.** In a chat or on the board of a
  room, select a message and press ^O. The node page opens, the same page that the
  Contacts list opens. For a message of yours, the page of your own node opens. The key
  works only when the author is one of your contacts. On the handhelds, the chip is
  `Author`, on Shift+F3.

### Changed

- **^L is now the map key, and ^G logs in to a room.** On the map and in the mesh walk, ^L
  goes back to your own node. That key was ^Y. On the board of a room, ^G logs in. That
  key was ^L.
- **A channel, a direct chat, and a room have the same F-key chips.** On the handhelds,
  F1, F2, and F3 are `Paths`, `URL→QR`, and `Map` on the three screens. With Shift, these
  keys hold what only one screen has: `Retry` or `Resend`, and `Log in` on a room. The
  `Day ↑` and `Day ↓` chips are gone. On the desktop, ^PgUp and ^PgDn still move from one
  day to the next.
- **A name that two nodes use at the same time is grey.** A channel message names its
  sender only by the name that the sender typed, and any node can use any name. When two
  nodes used the same name within a week, MeshTerm cannot know which one wrote the
  message. Thus the name is grey, as an unknown node is, and ^L and ^O do not open
  anything for it. When the old node has been quiet for longer than that, the name goes
  to the node heard most recently, because a radio that is flashed again gets a new
  identity. Before, MeshTerm chose one of the two nodes, often the old one. The new
  **Shared names** preference on the Preferences page sets the week.
- **Chips show where a route starts and where it ends.** Where your terminal draws path
  chips, the author of a message has a point on the right of its chip. A mention is a chip
  with a notch on its left. The packet viewer, the live feed, the Messages list, and the
  courier show the sender and the recipient in the same way.
- **Enter on the live feed keeps it on the newest packet.** When the live feed follows new
  packets (the pin at the top), Enter opens the packet viewer, and the viewer also follows
  new packets. The feed stays on the pin. Before, Enter stopped the feed on one packet.
  Enter on a packet that you selected still opens that packet and holds it.

### Fixed

- **The page of a contact shows its map preview again.** On a terminal of 24 rows, the
  action rows left too little space, and the preview was gone. Now the preview keeps its
  size and the page scrolls. Only the short display of the Cardputer Zero leaves it out.
- **The PicoCalc's radio recovers when a byte is lost on its serial line.** One lost or
  damaged byte could make the XIAO wait for data that never came. Then each start of
  MeshTerm said "No response from meshcore node". The firmware that the PicoCalc guide
  builds now drops an incomplete command after 200 ms of silence. To get this repair, build
  and flash the firmware again, as the guide says. The steps of the guide that install
  PlatformIO and flash the XIAO are also correct now.

## [0.10.3] - 2026-10-07

**You can join room servers, move your channels to a new device, and find the public
channels that MeshTerm heard. On the handhelds, paths are chips, QR codes are braille, and
the map no longer freezes.** MeshTerm now logs in to a room server, reads its board, and
posts to it. The Channels page exports and imports all your channels, and its channel guide
adds a public channel that MeshTerm heard on the mesh. You can hold Esc to quit. The
Cardputer Zero runs MeshTerm for the first time, but it is not supported yet.

### Added

- **You can join a room server, read its board, and post to it.** A room server is a message
  board on a radio. The new **Rooms** page, next to Channels, lists each room server that
  your radio knows, with the rooms that you joined first. Each room has a page: join, log in
  again, change the password, open the board, forget the room, or delete its stored posts.
  MeshTerm asks for the room password one time and remembers it. On the board, each post
  shows under the member who wrote it. Joined rooms are in Chat, and their new posts count
  toward the unread badge. A read-only member sees why the room drops their posts, instead
  of a compose line. A room does not answer a wrong password. Thus, when a login gets no
  answer, MeshTerm says what it knows and offers one next step: try by flood, try again, or
  add the room as a contact first. ^L logs in again (F6 on the handhelds). The new
  preference `room_relogin_minutes` (30 by default) sets how long a room can be quiet before
  MeshTerm logs in again by itself. On the command line, use `meshterm rooms list`,
  `rooms join`, and `rooms forget`. `chat send --to` posts to a room, and `chat history`
  names the author of each post. To try it with no radio, `meshterm --mock` has a room,
  Lakeside BBS, with the password `hello`.
  ([#24](https://github.com/jpmartineau/MeshTerm/issues/24))
- **You can export your channels to a file, and import them on another device.** A new
  device has only Public. On the Channels page, the new section "Export and import" has two
  rows. **Export channels** writes each channel to one file, with its key, its send scope,
  and its mute. **Import channels** reads the file on the new device. It shows its plan
  first, then it adds the channels in the order of the file. It never removes a channel.
  The file holds the keys of your private channels, so keep it private. A file from
  `config backup` also imports. On the command line, use `meshterm channels export FILE` and
  `meshterm channels import --file FILE`.
  ([#25](https://github.com/jpmartineau/MeshTerm/issues/25))
- **The channel guide lists the public channels that MeshTerm heard, and adds one with
  Enter.** It is the first row of "Add a channel" on the Channels page. The channels that are
  not on your device come first. Each row shows the number of messages heard, the age of the
  last one, and seven days of activity. A packet gives its channel only as a hash, so
  MeshTerm tries names: the channels of your device, each `#name` in a stored message, and
  each region that it knows. A guessed name must match two messages or more. A private
  channel never shows. On the command line, use `meshterm channels guide`.
  ([#25](https://github.com/jpmartineau/MeshTerm/issues/25))
- **You can hold Esc to quit.** Where MeshTerm can see that the Esc key is held, a hold of one
  second shows a dialog, and a hold of three seconds quits. This works in a Windows terminal,
  on a Linux console where MeshTerm can read the keyboard (the PicoCalc and the uConsole), in
  the emulator, and on the Cardputer Zero. A tap of Esc still goes back one screen. On macOS
  and over SSH, Esc does not change. MeshTerm also quits cleanly when the system sends it
  SIGTERM, for example at shutdown.
- **`meshterm emulate` shows the display of a handheld in a window on your desktop.**
  `meshterm emulate picocalc-lyra --mock` and `meshterm emulate cardputer-zero --mock` draw
  MeshTerm as that handheld draws it, with its font, its colours, and its F-key lane. Run
  `meshterm emulate --fetch-fonts` one time first. It copies the limits of the display and
  of the keyboard. It does not run the processor or the system of the handheld.
- **On the PicoCalc, each link in a chat message has a QR code under the message.** Point a
  phone at the code to open the link. On the desktop and on the Cardputer Zero, select a
  message and press ^U to show its links as QR codes, one at a time. ←→ go to the next code.
- **A link with no `https://` is a link too.** `meshterm.net/map` and `www.example.org` now
  open and get a QR code, but `file.txt` stays text. MeshTerm knows the real top-level
  domains from the list of IANA.
- **On a page that is taller than the screen, the arrows reach its top and its bottom.** With
  the highlight on the first row, ↑ scrolls the page up one line. With the highlight on the
  last row, ↓ scrolls it down. Thus the text above the first row and below the last row is
  always available. Home and End go to the ends of the page. This applies to each screen
  with a highlight. On the trace screen, ↓ now goes on into the results.
- **MeshTerm runs on the M5Stack Cardputer Zero, but it is not supported yet.** It draws on
  the display itself, opens from the app launcher of the device, and reads its battery. The
  Cap LoRa-1262 works with `--spi` and no setup. When you set the custom variable `gps` to
  `1`, the GPS of the Cap moves your node. For now, you must install MeshTerm from the source
  code. Read [the Cardputer Zero page](https://github.com/jpmartineau/MeshTerm/blob/main/docs/devices/cardputer-zero.md)
  before you try it.

### Changed

- **A path chip shows the colour of its node on a darker ground of the same colour.** Before,
  a chip had dark text on the colour of the node. Thus the colour moved from the name to the
  ground. Now a node has one colour in a list, in an arrow path, and in a chip.
- **The PicoCalc and the Cardputer Zero draw paths as chips.** Before, they drew arrows. On
  the PicoCalc, run the console font script again to get the two new glyphs (refer to
  [the PicoCalc page](https://github.com/jpmartineau/MeshTerm/blob/main/docs/devices/picocalc-lyra.md)).
- **On the PicoCalc and the Cardputer Zero, each QR code is drawn in braille.** A code is half
  the size, so a contact card and its link fit on one screen. On the handhelds, the braille
  dots now fill their cell, so the charts and the map draw solid lines.
- **The handhelds have new platform names: `picocalc-lyra` and `cardputer-zero`.** If you use
  `--platform picocalc` or `MESHTERM_PLATFORM=picocalc`, change it to `picocalc-lyra`.
  MeshTerm refuses the old name. MeshTerm finds the PicoCalc by itself, so most users have
  nothing to change.
- **^Y finds your own node on the map and in the mesh walk.** It was ^U, which now shows the
  QR codes of a chat message.
- **The trace screen draws the link of each hop as a path line.** A long name no longer
  pushes the table past the edge of the screen.
- **The device screen shows each field whole.** A long address no longer cuts the name. The
  address is last, and ←→ scroll a wide row.
- **The documents are in ASD-STE100 Simplified Technical English**, with Canadian spelling.
  Short sentences and one word for each concept make them easier to read, also when English
  is not your first language. The documents are now in folders: `docs/guide`, `docs/cli`,
  `docs/devices`, and `docs/development`.
- Section headings are dark grey, not the colour of the frame.
- The chat list reads CONVERSATION, NEW, LAST, and MESSAGE. The Channels list reads NEW over
  the unread count.
- On the desktop, the map uses the full width between the side borders.
- The legend of the node types says "room" instead of "room server". Thus it fits on one
  line on a handheld.
- On the PicoCalc, F3 in the quit dialog quits, because F3 on the main menu opened it.
- On the F-key lane, the scope chip and the window chip show only the name, so a region name
  gets all six cells.
- A chart keeps a fixed width for its axis labels. Thus it no longer moves sideways when the
  peak grows.
- The Support page sends bug reports to the Discord server.
- On the uConsole, the radio of the AIO shows as "uConsole AIO".

### Fixed

- **A full battery shows 100% on Linux and macOS.** A full T1000-E showed 9 or 10%. The
  battery was correct, but MeshTerm drew one column past the right edge of the terminal, and
  the last digit was lost. Only the Windows console needs that extra column.
- **More direct messages and room posts show as delivered.** MeshTerm waited approximately
  6 s for the acknowledgement of a flood, but a flood from some hops away takes 10 s. Also,
  an acknowledgement that came late was lost. Now MeshTerm waits approximately 15 s for a
  flood, and a late acknowledgement still marks the message as delivered. The Courier no
  longer sends a message again when its acknowledgement only came late.
- **The map no longer freezes the PicoCalc when you pan or zoom quickly.** The decoded tiles
  filled the memory of the device, and the device then used its swap on the SD card. Now
  the map keeps its tiles in a memory budget, fills large areas faster, and checks the
  serial port away from the screen. In a test on the device, the longest stop went from
  11.6 s to 0.6 s. A zoom also no longer shows a black screen between the two views.
- **The device list on the PicoCalc responds at once.** Each key press read the logo from the
  disk again.
- **A connection that does not open says why.** The reconnect dialog shows the reason after a
  second failure. A device that you name on the command line shows its error before the
  menu opens. A TCP failure names its cause: no such host, nothing on the port, no route, no
  answer, or the companion closed the connection. A Linux Bluetooth error keeps its full
  text.
- **On Linux, a companion with a PIN that closes the link asks for its PIN in seconds.**
  Before, the connection waited 60 s and did not ask. A link that drops while MeshTerm reads
  its services is tried again.
- **An SPI radio finds its GPIO chip by its label.** The number of the chip changes with the
  Compute Module and the kernel. Thus the uConsole AIO failed on some CM5 kernels.
- **A command in Repeater admin no longer takes a room post as its answer.** Before, any
  direct message that arrived during the command was read as the answer of the node.
- Repeater admin calls the guest password of a room server "Room password".
- The Trophy case draws each hop at the position of its contact, as the scoring does. One hop
  was drawn 38 km from its real place.

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

[Unreleased]: https://github.com/jpmartineau/MeshTerm/compare/v0.11.5...HEAD
[0.11.5]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.11.5
[0.11.4]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.11.4
[0.11.3]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.11.3
[0.11.2]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.11.2
[0.11.1]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.11.1
[0.11.0]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.11.0
[0.10.3]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.10.3
[0.10.2]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.10.2
[0.10.1]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.10.1
[0.10.0]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.10.0
[0.9.0]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.9.0
