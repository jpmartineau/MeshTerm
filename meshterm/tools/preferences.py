# SPDX-License-Identifier: Apache-2.0
"""The ``preferences`` tool: how MeshTerm itself behaves.

Interactively it opens the Preferences page (see :mod:`meshterm.ui.preferences`), which
stages changes and hands them back here to write. On the CLI it exposes the same
preferences as ``show`` / ``get`` / ``set`` / ``reset``, so a value can be changed from a
shell — or from a script setting a machine up — without launching the full-screen session.

Everything funnels through :meth:`PreferencesTool.run`, which applies a list of operations
and saves the file once at the end: one write per invocation, whether it came from the page
staging eleven changes or from a single ``preferences set``.

This is deliberately the *app's* tool, not the radio's. It reads nothing from the
companion, transmits nothing, and works with no device attached at all — which is why it
leads the **This app** section (the third owner, after this node and other nodes) rather
than sitting beside Device config.
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
    """View and change MeshTerm's own preferences, saved to ``preferences.toml``."""

    name = "preferences"
    title = "Preferences"
    icon = "⚙"
    help = "How MeshTerm behaves — startup, sending, history, …"
    category = "This app"
    order = 5  # leads the section: the one row there that changes MeshTerm, not describes it

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the Preferences page and collect the changes it staged.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"ops": [("set", key, value), …]}`` to write, or ``None`` if the reader left
            with nothing staged.
        """
        from ..ui.preferences import edit_preferences

        staged = await edit_preferences(ctx)
        if not staged:
            return None
        return {"ops": [("set", key, value) for key, value in staged.items()]}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Apply a list of preference operations, saving the file once if anything changed.

        Operations are ``("show",)``, ``("get", key)``, ``("set", key, value)``, and
        ``("reset",)``. They run in order, so a scripted ``set`` after a ``reset`` lands on
        top of the defaults rather than under them.

        Args:
            ctx: Shared application context.
            params: ``ops`` — the operations to perform.

        Returns:
            A :class:`ToolResult` counting the preferences changed.

        Raises:
            PreferenceError: If a key is unknown or a value fails its spec — reported
                cleanly by the CLI rather than as a traceback.
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
                # The bare value and nothing else — the caller named the key, and
                # whether it is overridden is what `show`'s DEFAULT lane answers. The
                # document keeps both, since a parser has no lane to read them off.
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
            # Switching the weekly advert on starts its week rather than sending, so the
            # instant is recorded as it happens — from the page and a shell alike — and
            # not whenever the scheduler next looks (see meshterm.core.advert_store).
            ctx.advert_store.set_enabled(bool(prefs.weekly_flood_advert))
        if not scripted and any(c["key"] == consolefont.PREFERENCE_KEY for c in applied):
            # The one preference whose effect is outside the app: the console's font is
            # the console's, so it takes hold now rather than next launch. The kernel
            # sends SIGWINCH on a VT font change, so prompt_toolkit repaints the whole
            # frame at the new row count by itself. Menu-only — a scripted run was typed
            # into a console it does not own (see meshterm.services.consolefont).
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
    """A preference's value in the form ``preferences set`` accepts back.

    :func:`~meshterm.core.preferences.format_value` writes for the page: a number carries
    its unit (``5 s``), so a bare figure in the VALUE lane still says what it counts. That
    unit is exactly what :func:`~meshterm.core.preferences.parse_value` will not take
    back, so the scripted dump drops it. Booleans keep ``on``/``off`` and an enum keeps
    its own key, both of which do round-trip.

    Args:
        spec: The preference the value belongs to.
        value: Its current value.

    Returns:
        The scripted rendering.
    """
    if spec.value_type == "bool":
        return "on" if value else "off"
    if spec.value_type in ("int", "float"):
        return f"{value:g}"
    return str(value)


def _row(prefs: Preferences, spec: PrefSpec) -> dict[str, Any]:
    """One preference as a report row, in the shared setting shape.

    ``overridden`` is the fact the plain face makes a reader derive by comparing two
    columns; a document that carried the same two numbers and left the comparison to the
    consumer would be handing over homework it has the answer to.
    """
    from ..ui.fields import Rendered

    value = prefs.get(spec.key)
    return {
        "key": spec.key,
        "value": Rendered(value, _script_value(spec, value)),
        "type": spec.value_type,
        # The reader's word for an enum's own key — never what `set` takes, which is the
        # key itself and rides in `value`.
        "label": (spec.choices or {}).get(value) if spec.value_type == "enum" else None,
        "redacted": False,
        "default": Rendered(spec.default, _script_value(spec, spec.default)),
        "overridden": prefs.is_overridden(spec.key),
        "help": spec.description,
    }


def _columns() -> tuple:
    """The preference row's columns, shared by ``show`` and ``get``."""
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
    """Every preference, its value, its built-in default, and what it is for.

    ``DESCRIPTION`` comes straight from :attr:`PrefSpec.help` — the text is already
    written, one short line each, and as the *last* column it can never disturb an
    alignment or wrap. It used to be cut for parser hygiene, which is a bargain the plain
    face no longer has to make: a listing is read by a person now, and a person choosing a
    preference is exactly who that sentence was written for.

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
    """One preference: the bare value plain, the whole setting object in the document."""
    from ..ui.report import BARE, Facts

    return Facts(
        key="preference",
        fields=_columns(),
        values=_row(prefs, spec),
        shape=BARE,
        bare="value",
    )


def _written(applied: list[dict[str, Any]], prefs: Preferences) -> Facts:
    """What ``set`` or ``reset`` changed — nothing plain, a record for whoever wants one.

    The plain face says it in the exit status and in the acknowledgement on stderr, which
    is the whole answer for a person. A caller managing a machine's configuration wants to
    know *what moved*, and had no way to ask.
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
