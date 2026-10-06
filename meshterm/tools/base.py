# SPDX-License-Identifier: Apache-2.0
"""The contract and the registry of the tool plugins.

A *tool* is one menu item and one CLI subcommand. To make a tool, subclass :class:`Tool`,
decorate it with :func:`register`, and implement :meth:`Tool.run`. The same registry
controls the interactive menu and the Typer CLI. :meth:`Tool.execute` puts each run of a
tool in a logged ``runs`` row. Thus a new tool gets storage and logging with no more
work.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core import exitcodes

if TYPE_CHECKING:  # do not import typer or the context at module load, for a fast start
    import typer

    from ..context import AppContext
    from ..ui.report import Report


@dataclass(slots=True)
class ToolResult:
    """The result of a run of a tool.

    Attributes:
        summary: A summary that can be serialized to JSON. It is stored in the record of
            the run.
        message: An optional closing message for a person. It is the closing line of the
            *menu*. A scripted run does not print it (refer to
            :func:`~meshterm.cli._execute_and_render`), because on the command line it
            repeats the output above it and the exit status below it.
        artifacts: The paths of the files that the tool made (for example, generated
            visualizations).
        exit_code: The status with which a scripted run must exit. Refer to
            :mod:`meshterm.core.exitcodes`. A tool that worked keeps ``OK``. A tool that
            ran correctly and found nothing to report sets ``NO_RESULT``. Thus a caller
            can tell an empty mesh from a full one, and does not have to count lines. A
            *failure* is raised, not returned, so this value never holds one.
        report: **The answer**, as data (refer to :mod:`meshterm.ui.report`). A scripted
            run puts it here, and the CLI boundary gives it to a renderer. The boundary
            does the same with ``exit_code``: the tool says what occurred, and other code
            changes that into bytes or into a process status. Before, the tool printed
            the answer instead. Then the answer was gone before anything could ask for it
            in another format. That is why ``--json`` got to two commands and stopped.

            ``None`` for the menu path, and for a feature that has no scripted face (the
            map, the dashboard). These continue to work with no change.

            It is not part of ``summary``. ``summary`` is the record in the *run log* of
            what this run did, and each run writes it to the ``runs`` table, also a menu
            run. If ``report`` is part of it, a five-hour ``monitor`` capture goes into
            SQLite. That is the cost of one field fewer.
    """

    summary: dict[str, Any] = field(default_factory=dict)
    message: str | None = None
    artifacts: list[str] = field(default_factory=list)
    exit_code: int = exitcodes.OK
    report: Report | None = None


class Tool(ABC):
    """The base class for all MeshTerm features.

    Class Attributes:
        name: The CLI identifier (kebab-case). It is unique among the tools.
        title: The name that the interactive menu item and the result screen of the tool
            show. It starts with a capital letter. It is the same as the name of the
            screen that the item opens, so that the menu never shows one name and opens
            another. If it is empty, ``name`` is used.
        help: The one-line description in the menu and in ``--help`` (with no period at
            the end).
        icon: The emoji that the interactive menu draws before the title (only there:
            the result screen keeps the plain title). Use only glyphs of one code point
            with emoji presentation, so that the width of two cells is the same on all
            terminals (refer to :mod:`meshterm.ui.tui.emoji_width`).
        category: The group label that organizes the interactive menu.
        order: The sort key in a category (a lower value comes first).
        menu_visible: Whether the tool is in the interactive menu. Set ``False`` for a
            tool that is only for the CLI (for example, diagnostics at startup, which
            have no place in a connected session). Such a tool registers a CLI
            subcommand as usual.
        popup: Whether the menu stays pushed while this tool runs. Then the prompts and
            the result of the tool float over the menu as modal dialogs (the pattern of
            the quit dialog), and do not replace the screen. This is for quick tools of
            the size of a dialog. A tool that pushes its own full screen keeps the
            default.
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
        """Do the work of the tool.

        Args:
            ctx: The shared application context (console, device, repository, and other
                items).
            params: The validated parameters for this run.

        Returns:
            A :class:`ToolResult` that describes the result.
        """

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any]:
        """Ask the user for the parameters of a menu run.

        The default implementation has no parameters. A tool overrides this method to ask
        the user through :attr:`AppContext.ui`: a dialog or a select list in the menu.
        On the CLI, it asks nothing, because the arguments came from the command line.

        Args:
            ctx: The shared application context.

        Returns:
            A dict of parameters that :meth:`run` accepts.
        """
        return {}

    def register_cli(self, app: typer.Typer) -> None:
        """Register this tool as a Typer subcommand.

        The default registers a command with no arguments. A tool with parameters
        overrides this method to declare typed options, and then calls :meth:`execute`.

        Args:
            app: The Typer application that gets the command.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _command() -> None:
            run_tool_command(self, {})

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run the tool, with a log of the run and a capture of its errors.

        This method opens a ``runs`` row before the run. After the run, it closes the row
        with the final status and the summary, also when the run failed.

        Args:
            ctx: The shared application context.
            params: The parameters for this run.

        Returns:
            The :class:`ToolResult` from :meth:`run`.

        Raises:
            Exception: Each error from :meth:`run`, raised again after it is stored.
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
            # When a tool refuses one of its own arguments (a `--to` that it cannot
            # resolve), it raises the same error as the parser. It is a usage error, not a
            # fault.
            typer.BadParameter,
            typer.Abort,
            typer.Exit,
        ) as exc:
            # An expected condition for the user (no device or an ambiguous device, a bad
            # config or preference value, a bad argument, or a temporary failure of a
            # command). Store it, but do not write a traceback. The callers print the
            # message cleanly.
            ctx.repo.finish_run(run_id, "error", {"error": str(exc)})
            ctx.log.debug("run %s aborted: %s", run_id, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - we store it, then raise it again
            ctx.repo.finish_run(run_id, "error", {"error": str(exc)})
            if is_connection_lost(exc):
                # The device was disconnected or switched off during a command. Each
                # layer above knows how to say that in one line. A stack of serial
                # internals tells how the driver found the problem, and that is a
                # different question.
                ctx.log.warning("run %s lost the device connection: %s", run_id, exc)
            else:
                ctx.log.exception("run %s failed: %s", run_id, exc)
            raise
        ctx.repo.finish_run(run_id, "ok", result.summary)
        ctx.log.debug("run %s ok", run_id)
        return result


_REGISTRY: dict[str, Tool] = {}

#: The explicit order of the tool categories in the menu. The menu asks "What would you
#: like to do?", so its sections answer that question. Each section names an *activity*,
#: not a subject, and holds all the items for that activity. Message is first (the
#: features for each day, and the list from which you select a recipient). Then come Watch
#: (what the mesh does, what it did, and what to get notifications about) and Explore
#: (where the nodes are, how they get to each other, and the boards that score the walks).
#: Then come the two owners of a setting: This node (the radio in your hand) and Other
#: nodes (the radio of another person, over the mesh).
#:
#: This app ends the list as the third and last *owner*, after the radio in your hand and
#: the radio of another person over the mesh: it is the program in front of you. It holds
#: the one item that changes how MeshTerm behaves (Preferences), and the four pages that
#: say what MeshTerm is. These are questions that no activity above can hold, and that
#: nothing on the mesh can answer. This app is last because the user who has nothing more
#: to do is the user who looks for it. Categories that are not in this list come last, in
#: alphabetical order. Thus a new category is always in the menu.
_CATEGORY_ORDER = [
    "Message",
    "Watch",
    "Explore",
    "This node",
    "Other nodes",
    "This app",
]


def _category_rank(category: str) -> int:
    """Return the sort rank of a category (its position in ``_CATEGORY_ORDER``, or last)."""
    try:
        return _CATEGORY_ORDER.index(category)
    except ValueError:
        return len(_CATEGORY_ORDER)


def register(cls: type[Tool]) -> type[Tool]:
    """A class decorator that makes an instance of a tool and adds it to the registry.

    Args:
        cls: The :class:`Tool` subclass to register.

    Returns:
        The class with no change (thus the decorator is transparent).

    Raises:
        ValueError: If the tool has no ``name``, or if the name is already registered.
    """
    instance = cls()
    if not instance.name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'name'.")
    if instance.name in _REGISTRY:
        raise ValueError(f"Duplicate tool name: {instance.name!r}")
    _REGISTRY[instance.name] = instance
    return cls


def all_tools() -> list[Tool]:
    """Return all the registered tools, sorted by category rank, then order, then name.

    :data:`_CATEGORY_ORDER` sets the order of the categories (Mesh first). Thus the main
    tools, Chat and Channels, come first in the menu. In a category, ``order`` and then
    ``name`` decide a tie.

    Returns:
        The registered tool instances, in a stable menu order.
    """
    return sorted(
        _REGISTRY.values(),
        key=lambda t: (_category_rank(t.category), t.category, t.order, t.name),
    )


def get_tool(name: str) -> Tool | None:
    """Find a registered tool by its name.

    Args:
        name: The ``name`` of the tool.

    Returns:
        The tool instance, or ``None`` if no tool has that name.
    """
    return _REGISTRY.get(name)
