# SPDX-License-Identifier: Apache-2.0
"""The tool plugin contract and registry.

A *tool* is one menu option / CLI subcommand. Subclass :class:`Tool`, decorate it with
:func:`register`, and implement :meth:`Tool.run`. The same registry drives both the
interactive menu and the Typer CLI, and :meth:`Tool.execute` wraps every invocation in a
logged ``runs`` row, so new tools get persistence and logging for free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core import exitcodes

if TYPE_CHECKING:  # avoid importing typer/context at module load for fast startup
    import typer

    from ..context import AppContext
    from ..ui.report import Report


@dataclass(slots=True)
class ToolResult:
    """The outcome of a tool execution.

    Attributes:
        summary: JSON-serializable summary persisted to the run record.
        message: Optional human-readable closing message. The *menu's* closing line — a
            scripted run does not print it (see
            :func:`~meshterm.cli._execute_and_render`), because on the command line it
            restates the output above it and the exit status below it.
        artifacts: Paths to any files produced (e.g. generated visualizations).
        exit_code: The status a scripted run should exit with — see
            :mod:`meshterm.core.exitcodes`. Left at ``OK`` by everything that worked;
            set to ``NO_RESULT`` by a tool that ran fine and found nothing to report, so
            a caller can tell an empty mesh from a full one without counting lines. A
            *failure* is raised, not returned, so this never carries one.
        report: **The answer**, as data (see :mod:`meshterm.ui.report`). A scripted run
            states it here and the CLI boundary hands it to a renderer, exactly as it
            already does with ``exit_code`` — the tool says what happened, and something
            else turns that into bytes or into a process status. It used to be printed
            instead, which meant the answer was gone by the time anything could ask for it
            in another format, which is why ``--json`` reached two commands and stopped.

            ``None`` for the menu path and for a feature with no scripted face at all (the
            map, the dashboard), which keep working unchanged.

            Not folded into ``summary``: that is the *run log's* record of what this
            invocation did, written to the ``runs`` table on every execution including a
            menu run. Putting a five-hour ``monitor`` capture in SQLite would be the price
            of one field fewer.
    """

    summary: dict[str, Any] = field(default_factory=dict)
    message: str | None = None
    artifacts: list[str] = field(default_factory=list)
    exit_code: int = exitcodes.OK
    report: Report | None = None


class Tool(ABC):
    """Base class for all MeshTerm features.

    Class Attributes:
        name: CLI identifier (kebab-case), unique across tools.
        title: Display name shown as the interactive menu item and on the tool's result
            window. Capitalized, and kept identical to the screen the item opens so the
            menu never promises one name and delivers another. Falls back to ``name``.
        help: One-line description shown in the menu and ``--help`` (no trailing period).
        icon: Emoji drawn before the title in the interactive menu (and only there — the
            result window keeps the plain title). Single-codepoint, emoji-presentation
            glyphs only, so the two-cell width holds across terminals (see
            :mod:`meshterm.ui.tui.emoji_width`).
        category: Grouping label used to organize the interactive menu.
        order: Sort key within a category (lower sorts first).
        menu_visible: Whether the tool appears in the interactive menu. Set ``False`` for
            CLI-only tools (e.g. startup-time diagnostics with no place in a connected
            session); such tools still register a CLI subcommand as usual.
        popup: Whether the menu stays pushed while this tool runs, so the tool's prompts
            and result float over it as modal popups (the quit-dialog pattern) instead of
            replacing the screen. For quick dialog-sized tools; a tool that pushes its
            own full screen keeps the default.
    """

    name: str = ""
    title: str = ""
    help: str = ""
    icon: str = ""
    category: str = "General"
    order: int = 100
    menu_visible: bool = True
    popup: bool = False

    @abstractmethod
    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Execute the tool's work.

        Args:
            ctx: Shared application context (console, device, repository, ...).
            params: Validated parameters for this invocation.

        Returns:
            A :class:`ToolResult` describing the outcome.
        """

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any]:
        """Interactively gather parameters for the menu.

        The default implementation requires no parameters. Tools override this to ask
        the user through :attr:`AppContext.ui` — a dialog or a select list in the menu, and
        nothing at all on the CLI, where the arguments came off the command line.

        Args:
            ctx: Shared application context.

        Returns:
            A parameter dict compatible with :meth:`run`.
        """
        return {}

    def register_cli(self, app: typer.Typer) -> None:
        """Register this tool as a Typer subcommand.

        The default registers a no-argument command. Tools with parameters override this
        to declare typed options, then delegate to :meth:`execute`.

        Args:
            app: The Typer application to add the command to.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _command() -> None:
            run_tool_command(self, {})

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run the tool wrapped in run-logging and error capture.

        Opens a ``runs`` row before execution and closes it with the final status and
        summary afterward, regardless of success.

        Args:
            ctx: Shared application context.
            params: Parameters for this invocation.

        Returns:
            The :class:`ToolResult` from :meth:`run`.

        Raises:
            Exception: Re-raises any error from :meth:`run` after recording it.
        """
        import typer

        from ..core.connection import DeviceCommandError, is_connection_lost
        from ..core.device_config import DeviceConfigError
        from ..core.preferences import PreferenceError
        from ..core.selection import DeviceSelectionError

        run_id = ctx.repo.start_run(self.name, params, ctx.profile_name)
        ctx.log.debug("run %s start: tool=%s params=%s", run_id, self.name, params)
        try:
            result = await self.run(ctx, {**params, "_run_id": run_id})
        except (
            DeviceSelectionError,
            DeviceConfigError,
            DeviceCommandError,
            PreferenceError,
            # A tool rejecting one of its own arguments (an unresolvable `--to`) raises
            # what the parser would have. It is a usage error, not a fault.
            typer.BadParameter,
            typer.Abort,
            typer.Exit,
        ) as exc:
            # Expected user-facing condition (no/ambiguous device, a bad config or
            # preference value, a bad argument, or a transient command failure): record it
            # but don't dump a traceback; callers print the message cleanly.
            ctx.repo.finish_run(run_id, "error", {"error": str(exc)})
            ctx.log.debug("run %s aborted: %s", run_id, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - we record then re-raise
            ctx.repo.finish_run(run_id, "error", {"error": str(exc)})
            if is_connection_lost(exc):
                # The radio was unplugged or powered off mid-command. Every layer above
                # already knows how to say that in one line; a stack of serial internals
                # describes how the driver found out, which is not the same question.
                ctx.log.warning("run %s lost the device connection: %s", run_id, exc)
            else:
                ctx.log.exception("run %s failed: %s", run_id, exc)
            raise
        ctx.repo.finish_run(run_id, "ok", result.summary)
        ctx.log.debug("run %s ok", run_id)
        return result


_REGISTRY: dict[str, Tool] = {}

#: Explicit menu ordering for tool categories. The menu asks "What would you like to
#: do?", so its sections answer that: each names a *doing*, not a subject, and holds
#: everything that shares it. Message leads (the everyday features, plus the book you
#: pick a recipient from), then Watch (what the mesh is doing, what it did, what to be
#: told about), Explore (where the nodes are, how they reach each other, and the boards
#: that score the walking), then the two owners a setting can belong to — This node, the
#: radio in your hand, and Other nodes, someone else's over the mesh.
#:
#: This app closes the list as the third and last *owner*, after the radio in your hand and
#: someone else's over the mesh: the program in front of you. It holds the one thing that
#: changes how MeshTerm behaves (Preferences) and the four pages that say what MeshTerm is
#: — questions none of the doings above can hold, and which nothing on the mesh can answer.
#: It sits last because a reader who has run out of things to do is who goes looking for it.
#: Categories not listed here sort last, alphabetically, so a new category still appears.
_CATEGORY_ORDER = [
    "Message",
    "Watch",
    "Explore",
    "This node",
    "Other nodes",
    "This app",
]


def _category_rank(category: str) -> int:
    """Return a category's sort rank (its position in ``_CATEGORY_ORDER``, else last)."""
    try:
        return _CATEGORY_ORDER.index(category)
    except ValueError:
        return len(_CATEGORY_ORDER)


def register(cls: type[Tool]) -> type[Tool]:
    """Class decorator that instantiates a tool and adds it to the registry.

    Args:
        cls: The :class:`Tool` subclass to register.

    Returns:
        The unchanged class (so the decorator is transparent).

    Raises:
        ValueError: If the tool has no ``name`` or the name is already registered.
    """
    instance = cls()
    if not instance.name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'name'.")
    if instance.name in _REGISTRY:
        raise ValueError(f"Duplicate tool name: {instance.name!r}")
    _REGISTRY[instance.name] = instance
    return cls


def all_tools() -> list[Tool]:
    """Return all registered tools sorted by category rank, then order, then name.

    Categories are ordered by :data:`_CATEGORY_ORDER` (Mesh first), so the first-class
    Chat and Channels tools lead the menu; within a category, ``order`` then ``name`` break
    ties.

    Returns:
        The registered tool instances in stable menu order.
    """
    return sorted(
        _REGISTRY.values(),
        key=lambda t: (_category_rank(t.category), t.category, t.order, t.name),
    )


def get_tool(name: str) -> Tool | None:
    """Look up a registered tool by name.

    Args:
        name: The tool's ``name``.

    Returns:
        The tool instance, or ``None`` if not registered.
    """
    return _REGISTRY.get(name)
