# SPDX-License-Identifier: Apache-2.0
"""The ``preferences`` tool: the behaviour of MeshTerm itself.

In the menu, the tool opens the Preferences page (refer to :mod:`meshterm.ui.preferences`).
The page stages the changes and returns them to this tool, which writes them. On the CLI,
the tool gives the same preferences through ``show``, ``get``, ``set``, and ``reset``.
Thus a user can change a value from a shell, and a script can change it when it sets up a
machine, without the full-screen session.

All changes go through :meth:`PreferencesTool.run`. It applies a list of operations and
saves the file one time at the end. Thus each run writes the file one time. This is true
for eleven changes that the page staged, and for one ``preferences set``.

This tool is the tool of the app, not of the device, on purpose. It reads nothing from the
companion, it transmits nothing, and it works with no device connected. For this reason,
it is the first row of the **This app** section (the third owner, after this node and
other nodes), and it is not next to Device config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core.preferences import (
    PREFERENCES,
    PreferenceError,
    Preferences,
    PrefSpec,
    format_value,
    get_spec,
)
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Block, Facts, Listing


@register
class PreferencesTool(Tool):
    """Show and change the preferences of MeshTerm, which are saved to ``preferences.toml``."""

    name = "preferences"
    title = "Preferences"
    icon = "⚙"
    help = "How MeshTerm behaves — startup, sending, history, …"
    category = "This app"
    order = 5  # first in the section. Its row changes MeshTerm, and the other rows describe it

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the Preferences page and collect the changes that it staged.

        Args:
            ctx: The shared application context.

        Returns:
            ``{"ops": [("set", key, value), …]}`` to write, or ``None`` if the user left
            with no staged changes.
        """
        from ..ui.preferences import edit_preferences

        staged = await edit_preferences(ctx)
        if not staged:
            return None
        return {"ops": [("set", key, value) for key, value in staged.items()]}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Apply a list of preference operations. If a value changed, save the file one time.

        The operations are ``("show",)``, ``("get", key)``, ``("set", key, value)``, and
        ``("reset",)``. They run in order. Thus a scripted ``set`` after a ``reset``
        changes the value from its default, and the ``reset`` does not remove that change.

        Args:
            ctx: The shared application context.
            params: ``ops``: the operations to do.

        Returns:
            A :class:`ToolResult` with the number of changed preferences.

        Raises:
            PreferenceError: If a key is unknown, or if a value does not agree with its
                spec. The CLI reports this as a clear error, not as a traceback.
        """
        from ..services import consolefont
        from ..ui.preferences import preferences_table
        from ..ui.surface import TuiUi

        prefs = ctx.preferences
        scripted = not isinstance(ctx.ui, TuiUi)
        ops: list[tuple] = list(params.get("ops") or [])
        blocks: list[Block] = []
        applied: list[dict[str, Any]] = []
        changes = 0
        for op in ops:
            action = op[0]
            if action == "show":
                if scripted:
                    blocks.append(_listing(prefs))
                else:
                    ctx.ui.show(preferences_table(prefs, ctx.console.width))
            elif action == "get":
                # Only the bare value, because the caller named the key. The DEFAULT lane
                # of `show` tells if the value is overridden. The document keeps both,
                # because a parser has no lane to read them from.
                spec = get_spec(op[1])
                if scripted:
                    blocks.append(_one(prefs, spec))
                else:
                    where = "changed" if prefs.is_overridden(spec.key) else "default"
                    shown = format_value(spec, prefs.get(spec.key))
                    ctx.ui.note(f"[accent]{spec.key}[/accent] = [brand]{shown}[/brand] ({where})")
            elif action == "set":
                before = prefs.get(op[1])
                after = prefs.set(op[1], op[2])
                if after != before:
                    changes += 1
                    applied.append({"key": op[1], "previous": before, "value": after})
            elif action == "reset":
                was = {s.key: prefs.get(s.key) for s in PREFERENCES if prefs.is_overridden(s.key)}
                changes += prefs.reset()
                applied.extend(
                    {"key": key, "previous": before, "value": prefs.get(key)}
                    for key, before in was.items()
                )

        if changes:
            prefs.save()
        if any(c["key"] == "weekly_flood_advert" for c in applied):
            # When the user switches the weekly advert on, its week starts, but nothing is
            # sent. Thus MeshTerm stores the instant when it occurs (from the page and from
            # a shell), and not when the scheduler next looks (refer to
            # meshterm.core.advert_store).
            ctx.advert_store.set_enabled(bool(prefs.weekly_flood_advert))
        if not scripted and any(c["key"] == consolefont.PREFERENCE_KEY for c in applied):
            # The only preference that has an effect outside the app. The font belongs to
            # the console, so the change occurs now, not at the next start. The kernel
            # sends SIGWINCH when the font of a VT changes. Thus prompt_toolkit paints the
            # whole frame again at the new row count by itself. This occurs only in the
            # menu, because a user typed a scripted run into a console that the run does
            # not own (refer to meshterm.services.consolefont).
            consolefont.apply(prefs.get(consolefont.PREFERENCE_KEY))
        if scripted and any(op[0] in ("set", "reset") for op in ops):
            blocks.append(_written(applied, prefs))

        plural = "" if changes == 1 else "s"
        message = (
            f"[ok]✓[/ok] saved [brand]{changes}[/brand] preference{plural} to "
            f"[accent]{prefs.path}[/accent]"
            if changes
            else None
        )
        return ToolResult(
            summary={"changes": changes}, message=message, report=tuple(blocks) or None
        )

    # -- CLI --------------------------------------------------------------------

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``preferences`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        prefs_app = typer.Typer(help=self.help, no_args_is_help=False, rich_markup_mode=None)

        @prefs_app.callback(invoke_without_command=True)
        def _root(ctx: typer.Context) -> None:
            """Show every preference when no subcommand is given."""
            if ctx.invoked_subcommand is None:
                run_tool_command(self, {"ops": [("show",)]})

        @prefs_app.command("show", help="Show every preference, its value, and its default")
        def _show_cmd() -> None:
            run_tool_command(self, {"ops": [("show",)]})

        @prefs_app.command("get", help="Print one preference's value")
        def _get_cmd(key: str = typer.Argument(..., help="Preference key")) -> None:
            run_tool_command(self, {"ops": [("get", key)]})

        @prefs_app.command("set", help="Set one preference to a value")
        def _set_cmd(
            key: str = typer.Argument(..., help="Preference key"),
            value: str = typer.Argument(..., help="New value"),
        ) -> None:
            run_tool_command(self, {"ops": [("set", key, value)]})

        @prefs_app.command("reset", help="Return every preference to its built-in default")
        def _reset_cmd(
            yes: bool = typer.Option(False, "--yes", help="Skip the confirmation"),
        ) -> None:
            if not yes:
                raise typer.BadParameter(
                    "resetting drops every preference you have changed; pass --yes to confirm"
                )
            run_tool_command(self, {"ops": [("reset",)]})

        app.add_typer(prefs_app, name=self.name)


__all__ = ["PreferencesTool", "PreferenceError"]


def _script_value(spec: PrefSpec, value: Any) -> str:
    """The value of a preference, in the form that ``preferences set`` accepts.

    :func:`~meshterm.core.preferences.format_value` writes for the page: a number has its
    unit (``5 s``), so that a bare number in the VALUE lane tells what it counts. But
    :func:`~meshterm.core.preferences.parse_value` does not accept that unit, so the
    scripted output removes it. A boolean keeps ``on`` or ``off``, and an enum keeps its
    own key. Both of these round-trip.

    Args:
        spec: The preference that the value is for.
        value: Its current value.

    Returns:
        The value as text for a script.
    """
    if spec.value_type == "bool":
        return "on" if value else "off"
    if spec.value_type in ("int", "float"):
        return f"{value:g}"
    return str(value)


def _row(prefs: Preferences, spec: PrefSpec) -> dict[str, Any]:
    """One preference as a report row, in the shared setting shape.

    On the plain face, the user compares two columns to find if a value is overridden.
    ``overridden`` gives this fact directly. If a document gives the same two numbers and
    lets the consumer compare them, the consumer must do work for which the document
    already has the answer.
    """
    from ..ui.fields import Rendered

    value = prefs.get(spec.key)
    return {
        "key": spec.key,
        "value": Rendered(value, _script_value(spec, value)),
        "type": spec.value_type,
        # The word that the user sees for the key of an enum. It is never what `set` takes:
        # `set` takes the key itself, which is in `value`.
        "label": (spec.choices or {}).get(value) if spec.value_type == "enum" else None,
        "redacted": False,
        "default": Rendered(spec.default, _script_value(spec, spec.default)),
        "overridden": prefs.is_overridden(spec.key),
        "help": spec.description,
    }


def _columns() -> tuple:
    """The columns of a preference row, which ``show`` and ``get`` share."""
    from ..ui import fields

    return (
        fields.word("key", "PREFERENCE"),
        fields.rendered("value", "VALUE"),
        fields.hidden("type"),
        fields.hidden("label"),
        fields.hidden("redacted"),
        fields.rendered("default", "DEFAULT"),
        fields.hidden("overridden"),
        fields.note("help", "DESCRIPTION"),
    )


def _listing(prefs: Preferences) -> Listing:
    """Each preference, with its value, its built-in default, and its purpose.

    ``DESCRIPTION`` comes directly from :attr:`PrefSpec.help`. That text is already
    written, with one short line for each preference. Because it is the last column, it
    can never break an alignment or cause a wrap. Before, the listing removed it, to keep
    the output clean for a parser. The plain face does not have to do that now: a person
    reads the listing, and the sentence was written for a person who selects a preference.

    Args:
        prefs: The preference set to report.

    Returns:
        The listing.
    """
    from ..ui.report import Listing

    return Listing(
        key="preferences",
        columns=_columns(),
        rows=[_row(prefs, spec) for spec in PREFERENCES],
        order=("PREFERENCE", "VALUE", "DEFAULT", "DESCRIPTION"),
    )


def _one(prefs: Preferences, spec: PrefSpec) -> Facts:
    """One preference: the bare value on the plain face, the full setting object in the document."""
    from ..ui.report import BARE, Facts

    return Facts(
        key="preference",
        fields=_columns(),
        values=_row(prefs, spec),
        shape=BARE,
        bare="value",
    )


def _written(applied: list[dict[str, Any]], prefs: Preferences) -> Facts:
    """What ``set`` or ``reset`` changed: nothing on the plain face, and a record for a log.

    The plain face gives the result in the exit status and in the acknowledgement on
    stderr. For a person, that is the whole answer. A caller that manages the setup of a
    machine wants to know what changed, and before, it had no way to ask.
    """
    from ..ui import fields
    from ..ui.report import SILENT, Column, Facts

    return Facts(
        key="applied",
        fields=(
            fields.integer("changes", "changes"),
            Column(key="applied"),
            fields.word("path", "path"),
        ),
        values={
            "changes": len(applied),
            "applied": applied,
            "path": str(prefs.path) if prefs.path else None,
        },
        shape=SILENT,
    )
