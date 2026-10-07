# MeshTerm documentation

This folder has everything that does not belong on the front page. The
[README](../README.md) is the front door. It says what MeshTerm is, how to install it, and
how to start a first session. This folder has the other documents, in four parts:

| Folder | What is in it |
| --- | --- |
| [`guide/`](#using-meshterm) | How to use MeshTerm: what each screen does, how to configure it, how to connect a companion, and terminals. |
| [`cli/`](#the-command-line) | The manual of the command line, and a cookbook of one-line commands. |
| [`devices/`](#running-it-on-a-handheld) | How to run MeshTerm on a handheld's own screen, with one manual for each device. |
| [`development/`](#reading-the-code) | How the code is laid out, how we write, and a historical survey. |

## Using MeshTerm

| Read | For |
| --- | --- |
| **[What MeshTerm does](guide/features.md)** | Each screen that the menu has, in the same groups as the menu, with the command that does the same job without the menu. |
| [Configuring MeshTerm](guide/configuration.md) | The difference between preferences and config, the device profiles, and all the files that MeshTerm keeps in `~/.meshterm`. |
| [When a companion does not connect](guide/connecting.md) | Bluetooth pairing on each system, how to find the PIN, and each connection error that MeshTerm reports, with the action to take. |
| [Terminals, icons, and the Windows console](guide/terminals.md) | Why the icons and charts draw correctly on some terminals and not on others, and what MeshTerm does about it on Windows. |

## The command line

| Read | For |
| --- | --- |
| **[The command line](cli/README.md)** | The manual. Each subcommand and option, what each prints on the plain face and on the JSON face, the exit statuses, and recipes that use `jq`. |
| [The CLI cookbook](cli/cookbook.md) | Common one-line commands, in groups by task. Use it when you want the form of a command and not its specification. |

## Running it on a handheld

Read these documents only if you want to run MeshTerm on a handheld's own screen. A
MeshCore companion on USB, Bluetooth, or Wi-Fi does not need them.

| Read | For |
| --- | --- |
| **[MeshTerm on hardware](devices/README.md)** | Start here. It says which of the manuals below is the correct one for you, and what the scripts on the device do. |
| [MeshTerm on the PicoCalc](devices/picocalc-lyra.md) | How to change a stock ClockworkPi PicoCalc into a Linux handheld that has a LoRa radio soldered inside: the shopping list, the change to a Luckfox Lyra core, Calculinux, and the radio. |
| [MeshTerm on the uConsole](devices/uconsole.md) | A uConsole (CM4 or CM5) with the hackergadgets AIO LoRa board. Its SX1262 is on the computer's own SPI bus. MeshTerm can control it directly with `--spi`. Or a small bridge can make it a companion for a node that is always on. |
| [MeshTerm on the Cardputer Zero](devices/cardputer-zero.md) | ⚠️ **Not supported yet.** What exists for M5Stack's Linux Cardputer, and how to try it in the desktop emulator. |

## Reading the code

| Read | For |
| --- | --- |
| [How the code is laid out](development/architecture.md) | The layers of the code, and where to put a new feature. |
| [How we write](development/writing-style.md) | How we write: the ASD-STE100 rules and the glossary for docstrings, comments, and documents. |
| [CLAUDE.md](../CLAUDE.md) | The real style guide: the terms, the UX standards, and the building blocks that you can use again. It is written for AI assistants, and it has the house rules for everyone. |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | How to propose a change, the rules for the use of AI, the dev install, the three checks that a change must pass, and what happens to the licensing of your contribution. |

### Historical record

This is a survey of the code. We keep it, because a project that writes down its own
problems is easier to trust than a project that does not. But it is a **snapshot of one
date, not a list of tasks**. Some of the problems that it describes are corrected now, and
the survey does not say which.

| Read | For |
| --- | --- |
| [UI consistency survey](development/survey-ui-consistency.md) | Each other point of the CLAUDE.md UX standard. Six sweeps found nothing, and this is the useful result. |

## Also here

`screenshots/` has the captures that the README shows. It also has the captures from which
[meshterm.net](https://meshterm.net) builds its landing page. Thus more than one place
refers to each file in this folder.
