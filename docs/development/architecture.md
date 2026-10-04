# How the code is laid out

A map of the package for anyone reading it for the first time. The house rules that
govern what goes where are in [CLAUDE.md](../../CLAUDE.md), which is the real style guide;
[CONTRIBUTING.md](../../CONTRIBUTING.md) has the dev install and the gates a change has to
pass.

MeshTerm is layered so a new feature is one file in `meshterm/tools/`:

```
cli.py        Typer app; no subcommand -> interactive menu
context.py    AppContext (console, config, repository, device) dependency container
core/         Domain: models, preferences + device profiles, connection abstraction (+ mock)
tools/        Pluggable "menu options"; each self-registers and gets logging for free
services/     Background algorithms (monitor, trace, tx search, courier, watchtower) — no UI
persistence/  SQLite schema, repository, structured logging
ui/           Rich theme/widgets + the full-screen TUI (screens, dialogs, map, chat)
```

Adding a feature means subclassing `Tool`, decorating it with `@register`, and
implementing `run()`. The same registry builds both the CLI and the menu, and the base
class wraps every execution in a logged `runs` row automatically.

