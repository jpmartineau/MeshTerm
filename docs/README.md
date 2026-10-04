# MeshTerm documentation

Everything that didn't belong on the front page. The [README](../README.md) is the front
door — what MeshTerm is, how to install it, and enough to get a first session running.
This folder is the rest, in four parts:

| Folder | What's in it |
| --- | --- |
| [`guide/`](#using-meshterm) | Using MeshTerm: what each screen does, configuring it, connecting a companion, terminals. |
| [`cli/`](#the-command-line) | The command line manual, and a cookbook of one-liners. |
| [`devices/`](#running-it-on-a-handheld) | Running MeshTerm on a handheld's own screen, one manual per device. |
| [`development/`](#reading-the-code) | How the code is laid out, and a historical survey. |

## Using MeshTerm

| Read | For |
| --- | --- |
| **[What MeshTerm does](guide/features.md)** | Every screen the menu offers, grouped the way the menu groups them, with the command that does the same job without it. |
| [Configuring MeshTerm](guide/configuration.md) | Preferences versus config, device profiles, and everything kept under `~/.meshterm`. |
| [When a companion won't connect](guide/connecting.md) | Bluetooth pairing on each system, finding the PIN, and every connection error MeshTerm reports, with what to do about it. |
| [Terminals, icons, and the Windows console](guide/terminals.md) | Why the icons and charts draw on some terminals and not others, and what MeshTerm does about it on Windows. |

## The command line

| Read | For |
| --- | --- |
| **[The command line](cli/README.md)** | The manual. Every subcommand and option, what each prints on the plain and JSON faces, the exit statuses, and recipes built on `jq`. |
| [The CLI cookbook](cli/cookbook.md) | Common one-liners by task, for when you want the shape of a command rather than its specification. |

## Running it on a handheld

Only if you want MeshTerm running on a handheld's own screen. A MeshCore companion on
USB, Bluetooth, or Wi-Fi needs none of this.

| Read | For |
| --- | --- |
| **[MeshTerm on hardware](devices/README.md)** | Start here — which manual below is yours, and what the device-side scripts do. |
| [MeshTerm on the PicoCalc](devices/picocalc-lyra.md) | A stock ClockworkPi PicoCalc to a Linux handheld with a LoRa radio soldered inside: the shopping list, the swap to a Luckfox Lyra core, Calculinux, and the radio. |
| [MeshTerm on the uConsole](devices/uconsole.md) | A uConsole (CM4 or CM5) with the hackergadgets AIO LoRa board, whose SX1262 sits on the computer's own SPI bus — MeshTerm drives it directly with `--spi`, or a small bridge fronts it as a companion for an always-on node. |
| [MeshTerm on the Cardputer Zero](devices/cardputer-zero.md) | ⚠️ **Not supported yet.** What exists for M5Stack's Linux Cardputer, and how to try it in the desktop simulator. |

## Reading the code

| Read | For |
| --- | --- |
| [How the code is laid out](development/architecture.md) | The layering, and where a new feature goes. |
| [CLAUDE.md](../CLAUDE.md) | The actual style guide — terminology, UX standards, and the reusable building blocks. Written for AI assistants, and the house rules for everyone. |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | How to propose a change, the rules on AI use, the dev install, the three checks a change must pass, and what happens to your contribution's licensing. |

### Historical record

A survey of the code, kept because a project that writes down its own problems is easier
to trust than one that doesn't — but it is a **snapshot of a date, not a to-do list**.
Some of what it describes has been fixed since, and it does not mark which.

| Read | For |
| --- | --- |
| [UI consistency survey](development/survey-ui-consistency.md) | Everything else in the CLAUDE.md UX standard — six sweeps found nothing, which is the useful part. |

## Also here

`screenshots/` holds the captures the README shows and the ones
[meshterm.net](https://meshterm.net) builds its landing page from, so a file in there is
referenced from more than one place.
