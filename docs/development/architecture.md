# How the code is laid out

This page is a map of the package for a person who reads it for the first time.
[CLAUDE.md](../../CLAUDE.md) has the house rules that decide what goes where. It is the real
style guide. [CONTRIBUTING.md](../../CONTRIBUTING.md) has the dev install and the checks
that a change must pass.

MeshTerm is divided into layers. Thus a new feature is one file in `meshterm/tools/`:

```
cli.py        Typer app; no subcommand -> interactive menu
context.py    AppContext (console, config, repository, device) dependency container
core/         Domain: models, preferences + device profiles, connection abstraction (+ mock)
tools/        Pluggable "menu options"; each self-registers and gets logging for free
services/     Background algorithms (monitor, trace, tx search, courier, watchtower) — no UI
persistence/  SQLite schema, repository, structured logging
ui/           Rich theme/widgets + the full-screen TUI (screens, dialogs, map, chat)
```

To add a feature, make a subclass of `Tool`, add the `@register` decorator to it, and
write the `run()` method. The same registry builds the CLI and the menu. The base class
automatically writes a logged `runs` row for each execution.
