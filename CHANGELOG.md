# Changelog

Notable changes to MeshTerm, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with
one caveat SemVer makes for a leading zero: while the major version is still `0`, a
**minor** bump is allowed to change how things behave, not just add to them.

## [Unreleased]

### Fixed

- `records --width` now rejects unsupported hash widths with a usage error instead
  of treating them as empty results or ignoring zero. Supported widths remain 1, 2, and 4.

### Changed

- **MeshTerm now sends at most one automatic advert a week, and none unless you ask.** A
  MeshCore companion never advertises by itself, so the hourly zero-hop and daily flood
  adverts MeshTerm sent by default were the only automatic adverts on the air, and more
  than a companion needs. They are gone from Device config, along with
  `config advert-cadence`. In their place is one preference, **Weekly advert** (off by
  default): once a device has gone a week without a flood advert, MeshTerm floods one —
  after it has heard the mesh, and then waited for **Advert quiet** seconds of silence
  (5, 15, 30, or 60; 30 by default) plus a random 0–5 s, so two waiting radios do not
  collide. Switching it on starts the week rather than sending; a flood advert sent by hand
  restarts it; each device keeps its own week, and one that falls due while MeshTerm is
  closed goes out the next time that device connects.

## [0.9.0] — 2026-09-22

**The first public release.** Everything in MeshTerm is new today, so instead of a list of
changes, here is what it does.

MeshTerm is a program you run in a terminal to work with a **MeshCore** radio — one of the
small LoRa boards people use to build long-range mesh networks. A mesh like that needs no
internet, no phone signal, and nobody in the middle: the radios pass messages along to each
other until they arrive. You plug one into your computer, start MeshTerm, and it shows you
what the mesh is doing.

### What you can do with it

- **Run it two ways, and it behaves the same either way.** Type `meshterm` on its own and
  you get a full-screen menu you drive with the arrow keys. Type a command instead —
  `meshterm contacts`, `meshterm info` — and it prints an answer and exits, which is what you
  want in a script. Add `--json` to nearly any command and the answer comes back as data
  for another program to read.

- **It listens all the time, and writes down what it hears.** Radios on a mesh announce
  themselves. From the moment yours is connected, MeshTerm notes down every announcement it
  overhears: which radio it was, how strong the signal was, and where it said it was. All of
  it is saved on your own computer. That is why the lists and charts can show you not only
  what is happening now, but what was happening last Tuesday.

- **You can see where everyone is.** There is a street map, drawn in the terminal, with the
  radios on it. You can pan it, zoom it, and search it. There is also a Time Machine: charts of
  everything you have ever overheard, so you can see when a radio usually talks and how
  well you have heard it, over a day, a week, a month or all of it.

- **You can talk to people.** Group channels and one-to-one messages, both live. If someone
  is out of range right now, the courier holds your message and delivers it when they come
  back. Channels can be shared as a QR code someone else scans, or as a link.

- **You can measure the mesh, not just guess at it.** Trace a message's route and see which
  hop is the weak one. Sweep your transmit power from coarse to fine to find the lowest
  setting that still gets through. Walk a map of how the whole mesh actually hangs together,
  which is often not how you thought it did.

- **It talks to your radio however your radio talks.** USB cable, Bluetooth, or over the
  network. It finds the ones it can find on its own and asks you to pick. If a connection
  drops it reconnects by itself. No radio yet? `meshterm --mock` runs the whole program
  against a simulated mesh, and records what it hears in a history database of its own,
  apart from your real one.

- **It runs on the small machines too.** The PicoCalc gets a layout of its own, built
  for its 53-column screen, with its own fonts, colours and on-screen key labels. The
  uConsole has room for the full desktop layout, and runs that.

- **It works with no internet.** Map tiles are kept on disk once fetched, and when there is
  no network at all the map falls back to a plain grid. Nothing about MeshTerm needs to
  phone home.

- **When something goes wrong, it can describe itself.** `meshterm diagnostics`, or the
  Diagnostics page in the menu, puts everything a bug report opens with in one block: which
  build this is and how it was installed, the operating system, the terminal and how big it
  is, and whether the radio answered. It names nothing private — no keys, no passwords, no
  positions, no contacts — and describes your mesh as counts rather than names. One keypress
  saves it to a file you can attach to an issue.

### Getting it

One file to download, with nothing else to install, for Windows, macOS (both Intel and
Apple silicon) and Linux (including 64-bit ARM, which covers the uConsole). If you would
rather use Python, `pipx install` works too, on Python 3.10 to 3.14. The README has the
exact commands.

MeshTerm is free and open source under the Apache 2.0 licence. The name and the logo are
not covered by the licence (see `NOTICE`), so a fork is welcome under its own name.

[Unreleased]: https://github.com/jpmartineau/MeshTerm/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.9.0
