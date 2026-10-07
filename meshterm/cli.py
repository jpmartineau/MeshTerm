# SPDX-License-Identifier: Apache-2.0
"""Typer entry point.

The callback parses the global options one time, and builds the shared
:class:`~meshterm.context.AppContext`. If there is no subcommand, MeshTerm starts the
interactive menu. If there is a subcommand, MeshTerm runs the subcommand of the selected
tool. Both paths go through :func:`run_tool_command` or :func:`run_menu`, so the behaviour
is the same.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import typer
from rich.console import Console
from typer.core import TyperGroup

from . import __version__
from .context import AppContext
from .core import exitcodes, win32dll
from .core.admin_store import AdminStore
from .core.config import MOCK_DB_FILENAME, Settings
from .core.connection import DeviceCommandError, is_connection_lost
from .core.device_config import DeviceConfigError
from .core.device_store import DeviceStore
from .core.instancelock import InstanceBusy, hold_instance_lock
from .core.preferences import (
    PREFERENCES_FILENAME,
    PreferenceError,
    Preferences,
    report_dropped,
)
from .core.selection import DeviceSelectionError
from .persistence.logging import configure_logging, get_logger, level_from_name, log_path
from .persistence.repository import Repository
from .platforms import Resolution, get_platform, resolve, set_platform, without_emoji
from .tools import all_tools
from .tools.base import Tool, ToolResult
from .ui import renderers, script
from .ui.renderers import OutputFormat
from .ui.termfont import emoji_support
from .ui.theme import make_console

#: The table of exit statuses, printed under each ``--help``. A documented return value is
#: one half of what makes the CLI scriptable (the other half is :mod:`meshterm.ui.script`).
#: A caller must not have to look for it in a README.
EXIT_STATUS_EPILOG = "Exit status: " + " · ".join(
    f"{code} {meaning}" for code, meaning in exitcodes.MEANINGS.items()
)


class _GlobalOptionsAnywhere(TyperGroup):
    """A group whose own options the user can type after the subcommand and before it.

    Click binds an option to the command that declares it. Thus a global option that the
    callback declares is a parse error one word later. ``meshterm contacts --json`` failed
    with a bare usage error, but ``meshterm --json contacts`` worked. This is the wrong
    order for a flag of the output format. In each tool that has such a flag, ``-o json``
    goes after the verb, and ``--json`` is the first option that anyone will try here.

    Thus the group moves its own options to the front before Click sees them, and both
    orders give the same run. ``--`` stops the moving, which is the standard way to say that
    the rest is data. ``--help`` is never moved on purpose, because ``contacts --help`` must
    stay the help of *contacts* and must not become the help of the program.
    """

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        """Move the options of this group out of ``args``, then parse as Click does."""
        return super().parse_args(ctx, _globals_first(self, args))


def _globals_first(group: TyperGroup, args: list[str]) -> list[str]:
    """``args`` with the options of ``group`` itself moved ahead of the subcommand.

    The function finds an option, and tells it from an argument, because the option has
    ``is_flag``. Typer includes its own copy of Click, so there is no ``click.Option`` that
    code can import to test against. The attribute asks the same question, and the code does
    not reach into a private package.

    Args:
        group: The command whose options count as global.
        args: The list of arguments as the user typed it.

    Returns:
        A list in a new order. The relative order of the options, and of all the other
        items, does not change. Thus a repeated flag resolves in the same way as in Click.
    """
    takes_value: dict[str, bool] = {}
    for param in group.params:
        if getattr(param, "is_flag", None) is None or param.name == "help":
            continue
        for opt in (*param.opts, *param.secondary_opts):
            takes_value[opt] = not param.is_flag

    lifted: list[str] = []
    kept: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            kept.extend(args[index:])
            break
        name = token.split("=", 1)[0]
        if name not in takes_value:
            kept.append(token)
            index += 1
        elif "=" in token or not takes_value[name] or index + 1 >= len(args):
            lifted.append(token)
            index += 1
        else:
            lifted.extend(args[index : index + 2])
            index += 2
    return lifted + kept


app = typer.Typer(
    cls=_GlobalOptionsAnywhere,
    add_completion=False,
    no_args_is_help=False,
    # The plain help of Click, not the rich help of Typer. There are no boxed panels for
    # Options and Commands, no colour, and no markup to remove from a piped `--help`. Scripts
    # read the CLI, and users read its help in the same way as the help of each other
    # utility (refer to meshterm.ui.script).
    rich_markup_mode=None,
    # The one description. It is the same as the package summary and the PyPI page. Refer to
    # `meshterm.__doc__`. If this text and that text are different, one of them is wrong.
    help=(
        "MeshTerm is a full-featured TUI MeshCore client for your terminal. "
        "Connects to a companion over USB, Bluetooth, or TCP. "
        "For Windows, macOS, and Linux."
    ),
    epilog=EXIT_STATUS_EPILOG,
)


def _print_version(value: bool) -> None:
    """Print the version and stop, when MeshTerm sees ``--version``.

    The option is eager. Thus it answers before the callback opens a database, resolves a
    platform, or looks for a radio. The version is true of the program and not of one run.
    The output is bare: ``meshterm <version>``. The caller asked for a version, and a script
    that reads it back has one field to take. It exits with ``0``, because to know the
    version is a success.
    """
    if not value:
        return
    script.console().print(f"meshterm {__version__}", highlight=False)
    raise typer.Exit(exitcodes.OK)


# The context that the callback builds and the subcommands use in one process.
_state: AppContext | None = None

# The platform resolution that the callback made. The ``platform`` diagnostic subcommand
# reports it (refer to :func:`platform_command`) and does not derive it again by itself.
_platform_resolution: Resolution | None = None


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Print the version and exit",
        callback=_print_version,
        is_eager=True,
    ),
    profile: str | None = typer.Option(None, "--profile", "-p", help="Device profile"),
    port: str | None = typer.Option(None, "--port", help="Serial port override"),
    ble: str | None = typer.Option(
        None, "--ble", help="Bluetooth address of a companion device (selects the BLE transport)"
    ),
    ble_pin: str | None = typer.Option(
        None, "--ble-pin", help="BLE pairing PIN, if the Bluetooth companion requires one"
    ),
    tcp: str | None = typer.Option(
        None,
        "--tcp",
        help="Network address host[:port] of a TCP companion (selects the TCP transport)",
    ),
    spi: bool = typer.Option(
        False,
        "--spi",
        help="Run a node on the LoRa radio on this machine's SPI bus (selects the SPI transport)",
    ),
    mock: bool = typer.Option(False, "--mock", help="Use the built-in simulator"),
    db_path: Path | None = typer.Option(
        None, "--db", help="SQLite database path (--mock records to meshterm-mock.db)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output"),
    absolute: bool = typer.Option(
        False, "--absolute", help="Print absolute timestamps instead of relative ages"
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress console logging"),
    platform: str | None = typer.Option(
        None,
        "--platform",
        help=(
            "Force the UI platform (regular|picocalc-lyra|cardputer-zero) "
            "instead of auto-detecting it"
        ),
    ),
) -> None:
    """Build the application context, then go to the menu or to a subcommand.

    Args:
        ctx: The Click or Typer context.
        version: Print the version and exit (:func:`_print_version` handles it eagerly).
        profile: The name of the device profile to use.
        port: An explicit serial port, which overrides the profile.
        ble: An explicit Bluetooth address, which selects the BLE transport.
        ble_pin: An optional BLE pairing PIN for the Bluetooth companion.
        tcp: An explicit network address ``host[:port]``, which selects the TCP transport.
        spi: Select the radio on the SPI bus of this machine. MeshTerm itself runs the node.
        mock: Whether to use the simulator instead of real hardware.
        db_path: Override the location of the database. If it is not set, a ``--mock`` run
            stores to the simulator's own database and not to the real history.
        json_output: Print the answer as JSON instead of aligned text.
        absolute: Print times as ISO-8601 instants instead of relative ages, for this run.
        quiet: Suppress console logging (logging to the file continues).
        platform: An explicit ``--platform`` override. Refer to
            :func:`meshterm.platforms.resolve`.
    """
    global _state, _platform_resolution

    # MeshTerm resolves and installs the platform first, before the code below it reads the
    # active platform (make_console, the theme, and the TuiSession that follows).
    try:
        _platform_resolution = resolve(platform)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--platform") from exc
    # MeshTerm asks here whether the terminal can draw emoji, and not in resolve().
    # resolve() answers "which flavour" from the flag, the environment, and the device
    # tree, and it must stay that pure. This is a limit of the terminal, and it applies to
    # the flavour that those three inputs chose. Refer to meshterm.ui.termfont.emoji_support.
    active = _platform_resolution.platform
    if not emoji_support().supported:
        active = without_emoji(active)
    set_platform(active)

    named_radios = [
        flag
        for flag, given in (("--port", port), ("--ble", ble), ("--tcp", tcp), ("--spi", spi))
        if given
    ]
    if spi and len(named_radios) > 1:
        # The SPI radio is on this machine. Each other flag names a radio that is not.
        raise typer.BadParameter(f"--spi and {named_radios[0]} name different devices; pass one")
    if mock and named_radios:
        # One flag says "pretend", and the other names one exact radio. `--mock` used to win
        # without a message. Thus a scheduled `--port COM7 --mock info` reported on a
        # simulator, and it looked as if it had reached the radio. Two claims that
        # contradict each other about the device to talk to are a bad command line. They are
        # not a question of precedence.
        raise typer.BadParameter(f"--mock and {named_radios[0]} name different devices; pass one")

    settings = Settings.load()
    if db_path is not None:
        settings.db_path = db_path
    elif mock:
        # The simulator is a fake radio, but it is not a fake MeshTerm. MeshTerm stored all
        # that the simulator sent as adverts to the real history, in the same way as
        # anything that a companion said. Then its four invented contacts appeared in the
        # mesh walk, the dashboard, and the map of the mesh that you really run. A pretend
        # radio gets a pretend history: ``<config_dir>/meshterm-mock.db``, which MeshTerm
        # creates at first use. ``--db`` still wins, because to name a database is a
        # deliberate claim about where a run stores its data. ``$MESHTERM_HOME`` is still the
        # only thing that isolates a whole run. The contact and channel caches, the outbox,
        # and the remembered devices are next to the database in each case (refer to
        # docs/cli/README.md).
        settings.db_path = settings.config_dir / MOCK_DB_FILENAME

    # There are two front ends and two consoles. This code decides which console the process
    # gets. A named subcommand is a scripted run. It prints on the plain console, without
    # colour and without wrapping, for which the output language of the CLI is written
    # (refer to meshterm.ui.script). If there is no subcommand, the menu runs, and it needs
    # the console with the theme.
    scripted = ctx.invoked_subcommand is not None
    console = script.console() if scripted else make_console()
    # MeshTerm loads the preferences before it configures logging, because one preference
    # sets how much goes into the file. It gives them to the context afterwards, so that
    # it does not read the file two times.
    prefs = Preferences.load(settings.config_dir / PREFERENCES_FILENAME)
    if absolute:
        # An override of the preference, and not a second switch next to it. By the rule of
        # this project, how MeshTerm behaves is a preference. A flag that bypassed the
        # registry would be a behaviour that a user cannot find. MeshTerm sets it and never
        # saves it, because it is a claim about this run.
        prefs.set("cli_time_format", "absolute")
    configure_logging(
        # The stdout of a scripted run carries its answer and nothing else. Thus the log
        # records go to stderr, and they do not mix with the data that a caller parses.
        script.stderr_console() if scripted else console,
        settings.config_dir,
        # A scripted run hears only about faults. MeshTerm already reports each expected
        # failure on stderr in one line and in the exit status (refer to `_reported`). If it
        # also printed a WARNING record, the same thing would appear two times. The file
        # keeps both in each case. The menu stays at INFO, because the log pane is the only
        # place where these records appear.
        level=logging.ERROR if scripted else logging.INFO,
        file_level=level_from_name(prefs.log_level),
        quiet=quiet or json_output,
    )
    # The file can hold values that no preference takes now. MeshTerm reports them only at
    # this point, because it read the file before there was a log to report them to.
    report_dropped(prefs, get_logger())

    if profile is not None and settings.resolve_profile(profile) is None:
        # If MeshTerm continued to the ordinary discovery, it would pick the radio that is
        # attached. Then a typing error in a scheduled `--profile yagi config advert` would
        # transmit from the wrong node, and would not fail. To name a profile is a claim
        # about which device to use, and a claim that MeshTerm cannot keep is a bad argument.
        known = ", ".join(sorted(settings.profiles)) or "none are defined"
        raise typer.BadParameter(f"no device profile named {profile!r} (known: {known})")

    app_ctx = AppContext(
        preferences=prefs,
        console=console,
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(settings.config_dir / "devices.json"),
        admin_store=AdminStore(settings.config_dir / "admin.json"),
        profile=settings.resolve_profile(profile),
        mock=mock,
        port_override=port,
        ble_override=ble,
        tcp_override=tcp,
        spi_override=spi,
        ble_pin=ble_pin,
        output=OutputFormat.JSON if json_output else OutputFormat.PLAIN,
        # Whether a tool can stop and ask. This is a different question from the question
        # of what its output looks like. A pipe on stdin means that nobody is there to answer.
        interactive=_stdin_is_a_person(),
        explicit_selection=(
            profile is not None or port is not None or ble is not None or tcp is not None or spi
        ),
    )
    _state = app_ctx
    ctx.call_on_close(app_ctx.repo.close)

    if ctx.invoked_subcommand is None:
        if json_output:
            # An interactive session cannot emit a document. If MeshTerm started the
            # full-screen menu for a caller that asked for JSON, the program that waits to
            # parse the JSON would hang.
            raise typer.BadParameter("--json needs a subcommand; the menu has no document")

        from .ui.menu import run_menu

        # This runs before prompt_toolkit takes the screen. It can change the font of the
        # console, or give the whole session to a better terminal. MeshTerm must draw the
        # full-screen frame one time, in the terminal where the session ends up.
        if _offer_a_console_that_can_draw_meshterm(console, prefs):
            return

        from .services import consolefont as vt_console_font

        # The whole console of the PicoCalc has one font. Thus a request for the 6x8 build
        # also changes the font of the shell. MeshTerm saves the font of the user before the
        # first paint, and puts it back when the app ends. It does this in the `finally`
        # here, and in an `atexit` handler that remember() arms, because ^Q and an unhandled
        # error leave through other paths. This is only the boundary of the menu. A scripted
        # run prints into a console that it was invited into, and it never changes the font.
        saved_font = vt_console_font.remember()
        vt_console_font.apply(prefs.get("console_font"))

        # The interactive session keeps the stores in memory and rewrites them whole. Thus it
        # is the session that claims the directory. One-shot subcommands are short and
        # mostly read data. If an open menu in another window blocked `meshterm contacts`,
        # that would be an obstacle and not a guard.
        # A SIGTERM (the end of a held Esc from the launcher of the Cardputer, or a system
        # that shuts down) makes the menu leave in the same way as Quit. It does not stop
        # the menu at the place where it was.
        from .services import hold_to_quit

        hold_to_quit.leave_on_sigterm()
        try:
            with hold_instance_lock(settings.config_dir):
                asyncio.run(_drive(run_menu(app_ctx), app_ctx))
        except InstanceBusy as exc:
            console.print(f"[err]✗[/err] {exc}")
            raise typer.Exit(code=1) from None
        except Exception as exc:
            if type(exc).__name__ == "NoConsoleScreenBufferError":
                # prompt_toolkit needs a real Win32 console. It reports that the console is
                # absent in the language of its own internals. A traceback looks like "this
                # app is broken", but the answer is "open a different terminal". Thus
                # MeshTerm gives that answer.
                get_logger().warning("no Windows console available: %s", exc)
                _report_no_windows_console(console)
                raise typer.Exit(code=1) from None
            # MeshTerm logs the error before it raises it again. In this case the log is
            # most important. A session that the user started with a double-click on the
            # executable owns its console window. When the session dies, the window closes
            # and the traceback goes with it. Then the file is the only thing to read.
            get_logger().exception("the interactive session raised an unhandled error")
            _report_log_location(console, settings.config_dir)
            raise
        finally:
            vt_console_font.restore(saved_font)


def _offer_a_console_that_can_draw_meshterm(console: Console, prefs: Preferences) -> bool:
    """On the classic Windows console, move the user to a better place.

    There are two remedies. MeshTerm does not ask about them in the same way.

    The first remedy is **Windows Terminal**, and MeshTerm does it without a question. It
    is the only place that shows the whole app, with the emoji icons. No font can put emoji
    on the classic console. The only Windows fonts that have emoji are proportional, and a
    console does not take them. The move installs nothing and changes nothing. If MeshTerm
    asked, it would ask someone who has not seen the app yet and cannot judge it, and the
    question has only one sensible answer.

    The second remedy is **a font**. MeshTerm uses it only where the move was not possible.
    MeshTerm does ask about this one, because it writes a file onto the machine of a person,
    and that person must agree to it.

    Args:
        console: The console to report and ask on.
        prefs: The preference set. The function reads it for a previous refusal and writes a
            new refusal to it.

    Returns:
        ``True`` when MeshTerm reopens the session in another place and this session must
        stop.
    """
    from .ui.termfont import classic_console

    if get_platform().own_display:
        return False
    if not classic_console() or prefs.get("console_setup") == "off":
        return False
    if _move_to_windows_terminal(console):
        return True
    _offer_a_font_this_console_can_draw(console, prefs)
    return False


def _move_to_windows_terminal(console: Console) -> bool:
    """Reopen the session in Windows Terminal, where the app can draw all of itself.

    MeshTerm does this and does not offer it. The classic console cannot show an icon, in
    all cases. Thus "reopen there?" is a question that has one sensible answer. It is asked
    of someone who has not seen the app yet and cannot judge it. It is also asked at the
    worst moment, before MeshTerm has drawn anything. The move costs nothing and changes
    nothing: no install, no setting, and one window instead of another.

    MeshTerm announces the move, and does not do it silently. The announcement names the
    way to stop it, because a program that opens a window that the user did not ask for
    must at least say so. If ``console_setup`` is ``off``, the session stays here always.

    Args:
        console: The console to report on.

    Returns:
        Whether Windows Terminal took over and this session must stop.
    """
    from .core import winterminal

    if winterminal.available() is None:
        return False

    console.print()
    console.print("[accent]•[/accent]  Opening in [accent]Windows Terminal[/accent] — this console")
    console.print("   can't draw MeshTerm's icons or charts.")
    console.print("[muted]   Preferences → Display → Console setup keeps it here instead.[/muted]")
    console.print()

    if winterminal.reopen():
        get_logger().info("reopened the session in Windows Terminal")
        return True
    console.print("[warn]⚠[/warn]  Windows Terminal would not start. Staying here.")
    get_logger().warning("could not start Windows Terminal")
    return False


def _offer_a_font_this_console_can_draw(console: Console, prefs: Preferences) -> None:
    """Offer a font that can at least draw the charts and marks, for a user who stays.

    The console has no font fallback. If its font does not have a glyph, the console shows
    a box. Its default font (Consolas, or Lucida Console under Windows PowerShell) does not
    have most of the glyphs that MeshTerm draws with. This includes each of the 44 braille
    cells that make the timelines.

    The offer has two forms, which depend on what is already there. A machine with Windows
    Terminal (and each Windows 11 machine) already has Cascadia, so the offer is only to use
    it. A bare Windows 10 gets the offer to install the copy that MeshTerm includes.

    If the user declines, MeshTerm answers with a plain description of what the refusal
    looks like, and asks one more time. A user who has never seen the app cannot know what
    "some glyphs may not render" will mean. MeshTerm remembers a second refusal in the
    ``console_setup`` preference. Thus it asks the question two times in total, and then
    never again.

    Args:
        console: The console to ask on.
        prefs: The preference set. The function reads it for a previous refusal and writes a
            new refusal to it.
    """
    from .core import consolefont
    from .ui.termfont import face_draws_charts, installed_chart_font

    if face_draws_charts(consolefont.current_face()):
        return

    installed = installed_chart_font()
    for asking_again in (False, True):
        if asking_again:
            console.print()
            console.print("[warn]⚠[/warn]  Then MeshTerm is going to look wrong here.")
            console.print()
            console.print("   Every chart — the signal timelines, the activity graphs — is")
            console.print("   drawn from braille characters this console's font does not")
            console.print("   have, so they will come out as rows of empty boxes. So will")
            console.print("   the ✓ and ✗ marks, and the ★ on the map.")
            console.print()
            console.print("   Nothing is broken and nothing is lost — the app works, and")
            console.print("   this is only about what the font can draw. It is your")
            console.print("   console, so it is your call.")
            console.print()
        elif installed:
            console.print()
            console.print("[accent]•[/accent]  This console's font can't draw MeshTerm's charts.")
            console.print(f"   You already have [accent]{installed}[/accent], which can.")
            console.print()
        else:
            console.print()
            console.print("[accent]•[/accent]  This console's font can't draw MeshTerm's charts.")
            console.print("   MeshTerm ships [accent]Cascadia Mono PL[/accent] — Microsoft's")
            console.print("   console font — and can install it just for you. No")
            console.print("   administrator rights, nothing downloaded, 723 KB.")
            console.print()

        verb = "Use it" if installed else "Install and use it"
        if _asks_yes(console, f"   {verb}? [y/N] "):
            _use_a_better_console_font(console, installed)
            return

    prefs.set("console_setup", "off")
    try:
        prefs.save()
    except (OSError, RuntimeError) as exc:  # a read-only config dir is not a fatal error here
        get_logger().warning("could not remember the console setup choice: %s", exc)
    console.print()
    console.print("[muted]   Leaving it as it is. Change your mind on the Preferences[/muted]")
    console.print("[muted]   page, under Display → Console setup.[/muted]")
    console.print()


def _use_a_better_console_font(console: Console, installed: str | None) -> None:
    """Install the font if it is necessary, select it, and say plainly what happened."""
    from .core import consolefont

    face = installed
    if face is None:
        if not consolefont.install_bundled_font():
            console.print("[err]✗[/err]  The font could not be installed. Leaving the")
            console.print("   console as it is.")
            get_logger().warning("bundled console font could not be installed")
            return
        face = consolefont.BUNDLED_FACE

    if consolefont.use(face):
        console.print(f"[ok]✓[/ok]  Now drawing with [accent]{face}[/accent].")
        get_logger().info("console font set to %s", face)
    else:
        # The font is installed but not selected. It is there for the next time, and for the
        # properties dialog of the console. MeshTerm says this and does not hide it.
        console.print(f"[warn]⚠[/warn]  {face} is installed, but this console kept its own")
        console.print("   font. It will be available the next time you open one.")
        get_logger().warning("console refused the font %s", face)


def _asks_yes(console: Console, prompt: str, *, default: bool = False) -> bool:
    """Ask a yes or no question on the console.

    This is not a TUI dialog. It runs before prompt_toolkit owns the screen, with the other
    reports on the plain console here. If nobody can answer the prompt (no stdin, or a
    closed pipe), the function uses the default. The default of each caller is the answer
    that costs the user the least.

    Args:
        console: The console to ask on.
        prompt: The question, which ends with its own spacing.
        default: The answer that an empty line means, and the answer that an unreadable stdin
            gives.

    Returns:
        Whether the user said yes.
    """
    console.print(prompt, end="")
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        console.print()
        return default
    if not answer:
        return default
    return answer in {"y", "yes"}


def _report_log_location(console: Console, config_dir: Path) -> None:
    """Name the log after a fault, because the log is the copy that outlives the terminal."""
    console.print()
    console.print(f"[muted]The details are in {log_path(config_dir)}[/muted]")
    console.print("[muted]Attach it to a bug report. For more detail next time, raise[/muted]")
    console.print("[muted]'Log detail' on the Preferences page.[/muted]")


def _report_no_windows_console(console: Console) -> None:
    """Explain that MeshTerm cannot draw in this console, and how to get a console that it can.

    The code gets here when prompt_toolkit finds no screen buffer of a Win32 console. This
    happens in a Git Bash, MSYS, or Cygwin shell. These announce themselves as ``xterm``,
    and they are not Windows consoles. It also happens in a process whose output was
    redirected away from a console.
    """
    console.print("[err]✗[/err] MeshTerm needs a Windows console, and could not find one.")
    console.print()
    console.print("That usually means one of two things:")
    console.print()
    console.print("  • You are in Git Bash, MSYS, or Cygwin, which are not Windows")
    console.print("    consoles. Open [accent]Windows Terminal[/accent] or")
    console.print("    [accent]PowerShell[/accent] and run it from there.")
    console.print("  • The output is piped or redirected somewhere. The full-screen menu")
    console.print("    needs the terminal itself.")
    console.print()
    console.print("Every screen also has a subcommand, and those pipe fine —")
    console.print("try [accent]meshterm --help[/accent].")


@app.command(
    name="platform",
    help="Show which platform MeshTerm runs as, and why",
)
def platform_command() -> None:
    """Print the resolved platform and each input that the resolution examined.

    This is a diagnostic for the platform seam (refer to ``meshterm/platforms.py``). It
    confirms the flavour that a given run would use, and why. It shows the ``--platform``
    and ``MESHTERM_PLATFORM`` inputs, the value of ``/proc/device-tree/model`` (``None`` off
    the real hardware), and the input that decided the result.

    It also reports the emoji decision for the terminal. This is a separate question from
    the flavour. It is the usual reason why a Windows session looks plainer than the
    screenshots (refer to :func:`meshterm.ui.termfont.emoji_support`).
    """
    from .ui import fields
    from .ui.report import Facts

    assert _platform_resolution is not None  # the callback sets it, and it always runs first
    assert _state is not None  # the same
    r = _platform_resolution
    emoji = emoji_support()
    renderers.for_format(_state.output, _state.console).render(
        (
            Facts(
                key="platform",
                fields=(
                    fields.word("platform", "platform"),
                    fields.word("platform_source", "platform_source"),
                    fields.word("flag", "flag"),
                    fields.word("env", "env"),
                    fields.word("device_tree_model", "device_tree_model"),
                    fields.flag("icons", "icons"),
                    fields.word("icons_source", "icons_source"),
                ),
                values={
                    "platform": r.platform.name,
                    "platform_source": r.source,
                    "flag": r.flag or None,
                    "env": r.env or None,
                    "device_tree_model": r.detected_model or None,
                    "icons": emoji.supported,
                    "icons_source": emoji.source,
                },
            ),
        )
    )


@app.command(
    name="specimen",
    help="Print every mark, icon, and colour MeshTerm draws",
)
def specimen_command() -> None:
    """Print the specimen of the visual language for the active platform.

    The card shows each mark, icon funnel, colour scale, and fold. It is drawn through the
    same theme and glyph code that the TUI uses. Thus on the console of the PicoCalc it is
    the acceptance screen for the font and the palette. With ``--platform picocalc-lyra`` on
    a desktop, it shows a preview of that flavour. Refer to :mod:`meshterm.ui.specimen`.

    This is the only command that keeps its colour. It builds its own console with the
    theme to do this (and does not use the plain console that each other subcommand prints
    on, refer to :mod:`meshterm.ui.script`). It is not scripted output that is also pretty.
    The colour is the output. A specimen without colour would test nothing.

    It is also the only command that refuses ``--json``, with a clear message and as a usage
    error. There is no data behind a colour card. A document of it would be a lie or an
    empty gesture. The project gives the same clear refusal for the map, the dashboard, and
    the live feed. These have no subcommand that could refuse.
    """
    from .ui.specimen import specimen_lines

    assert _state is not None  # the callback sets it, and it always runs first
    if _state.output is not OutputFormat.PLAIN:
        raise typer.BadParameter(
            "specimen has no machine-readable output — its output is the colour"
        )

    console = make_console()
    for line in specimen_lines():
        console.print(line)


#: Global options that the emulated MeshTerm must not inherit. The emulator names its own
#: platform, it refuses a machine-readable face, and it answers ``--version`` before
#: anything runs.
_NOT_FORWARDED = {"platform", "json_output", "version", "help"}


def _forwarded_globals(ctx: typer.Context) -> list[str]:
    """The global options that this run set, written again for the emulated MeshTerm.

    MeshTerm moves a global option ahead of the subcommand in all cases, wherever the user
    typed it (refer to :func:`_globals_first`). Thus ``meshterm emulate picocalc-lyra
    --mock`` parses ``--mock`` in the outer MeshTerm. The emulated MeshTerm is a second run,
    and it needs the same flags. Thus the function reads them from the parsed values of the
    root, and writes them out again.
    """
    root = ctx.find_root()
    out: list[str] = []
    for param in root.command.params:
        if param.name in _NOT_FORWARDED or not param.opts:
            continue
        value = root.params.get(param.name)
        if value is None or value == param.default:
            continue
        option = max(param.opts, key=len)
        if getattr(param, "is_flag", False):
            out.extend([option] if value else list(param.secondary_opts[:1]))
        else:
            out.extend([option, str(value)])
    return out


@app.command(
    name="emulate",
    help="Show MeshTerm as it looks on a handheld, in a window",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def emulate_command(
    ctx: typer.Context,
    device: str | None = typer.Argument(
        None, help="The handheld: cardputer-zero or picocalc-lyra", show_default=False
    ),
    scale: int = typer.Option(3, "--scale", min=1, help="Window zoom, in whole pixels"),
    fetch_fonts: bool = typer.Option(
        False, "--fetch-fonts", help="Download the Terminus font the emulator draws in, once"
    ),
    archive: Path | None = typer.Option(
        None, "--archive", help="Install the font from this downloaded release archive"
    ),
) -> None:
    """Run MeshTerm in a window that shows the display of a handheld, pixel for pixel.

    This is not a strict emulator. MeshTerm runs as itself on this machine. The window
    reproduces the display and the keyboard of the device: its display, its grid, its font,
    the colours that it can show, and the keys that drive its F-key lane (refer to
    :mod:`meshterm.emulator`). The command passes each global option that the user gives
    (``--mock``, ``--port``, ``--ble``…) to the MeshTerm that runs in the window.

    Like ``specimen``, it has no machine-readable face, because a window is not a document.
    Thus it refuses ``--json`` as a usage error.
    """
    from .emulator.__main__ import start

    assert _state is not None  # the callback sets it, and it always runs first
    if _state.output is not OutputFormat.PLAIN:
        raise typer.BadParameter("emulate has no machine-readable output — it opens a window")

    def complain(problem: str) -> None:
        script.stderr_console().print(f"meshterm: {problem}", style="err", highlight=False)

    if fetch_fonts or archive is not None:
        from .emulator.fetch import install_terminus

        try:
            install_terminus(archive, say=lambda line: print(line, file=sys.stderr))
        except (OSError, ValueError) as why:
            complain(f"couldn't install the font: {why}")
            raise typer.Exit(exitcodes.FAILURE) from None
        return
    try:
        status = start(
            device, [*_forwarded_globals(ctx), *ctx.args], scale=scale, complain=complain
        )
    except ValueError as why:
        raise typer.BadParameter(str(why), param_hint="DEVICE") from None
    if status:
        raise typer.Exit(status)


def run_tool_command(tool: Tool, params: dict) -> None:
    """Run a tool from a CLI subcommand and render its result.

    The function builds the device connection and takes it down again in one event loop.
    Thus the ``meshcore`` client is never used across loops.

    Args:
        tool: The tool to run.
        params: The parameters that MeshTerm parsed from the options of the subcommand.
    """
    assert _state is not None  # the callback sets it before any subcommand runs
    try:
        result = asyncio.run(_drive(_execute_and_render(tool, params, _state), _state))
    except DeviceSelectionError as exc:
        # MeshTerm could not pick any companion. It transmitted nothing, and a retry does
        # not help until the caller attaches a device or names one.
        raise _reported(tool, exc, exitcodes.NO_DEVICE) from exc
    except DeviceCommandError as exc:
        # MeshTerm reached a device, and the operation failed. A retry is useful.
        raise _reported(tool, exc, exitcodes.DEVICE) from exc
    except (DeviceConfigError, PreferenceError) as exc:
        # A value that the caller gave is wrong, for example a bad setting key or a
        # preference that is out of range. A retry changes nothing. The caller must change
        # what it asked for.
        raise _reported(tool, exc, exitcodes.FAILURE) from exc
    except (typer.BadParameter, typer.Abort, typer.Exit):
        # A tool rejected one of its own arguments (for example a `--to` that cannot be
        # resolved, or a malformed path). It raised the same exception that the parser
        # would raise. It is a usage error. Click already knows how to print it and which
        # exit status to use. It is not a fault. Thus it goes through, without a traceback
        # and without a pointer to the log.
        raise
    except Exception as exc:
        # If the serial link drops (the device is unplugged or loses power during a
        # command), a one-shot scripted run cannot recover, as the interactive menu can
        # offer. But the user gets a clean message and not a traceback, and the exit status
        # is a device failure.
        if is_connection_lost(exc):
            raise _reported(
                tool,
                "the connection to the device was lost (it may have been unplugged or powered off)",
                exitcodes.DEVICE,
            ) from exc
        # Each other exception is a real fault. It still reaches the terminal as a traceback,
        # because a person who looks at the terminal wants to see it. It also goes into the
        # log. The log is the copy that stays after the terminal closes, and it is the one
        # thing that is useful to attach to a bug report.
        get_logger().exception("%s raised an unhandled error", tool.name)
        _report_log_location(script.stderr_console(), _state.settings.config_dir)
        raise
    if result.exit_code != exitcodes.OK:
        raise typer.Exit(result.exit_code)


def _stdin_is_a_person() -> bool:
    """Whether somebody is there to answer a prompt.

    This is the test that a tool needs before it stops and asks. If stdin is redirected or
    piped, nobody watches, and a command that blocks there hangs and does not fail.
    ``tx-optimize`` used to check ``--json`` instead. That flag is about the output format.
    It answers a different question, and it gave a wrong answer to this question in both
    directions.

    On Windows, ``isatty`` alone is not enough, and the gap is exactly the case that this
    function exists for. ``NUL`` is a character device. Thus the empty stdin of a scheduled
    task answers yes. Then :func:`getpass.getpass` on Windows reads the console directly
    through ``msvcrt`` and not through stdin, and it waits forever for a key press that
    nobody is there to make. ``GetConsoleMode`` succeeds only on the handle of a real
    console, and that is the question that the function asks.

    Returns:
        ``True`` only where stdin is a terminal that somebody can type into.
    """
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):  # pragma: no cover - stdin is closed or replaced
        return False
    if sys.platform != "win32":
        return True
    try:  # pragma: no cover - the branch is for Windows only and needs a real handle
        import ctypes
        import msvcrt

        mode = ctypes.c_uint()
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        return bool(win32dll.kernel32().GetConsoleMode(handle, ctypes.byref(mode)))
    except Exception:  # pragma: no cover - there is no console, or kernel32 is a stub
        return False


def _reported(tool: Tool, problem: object, code: int) -> typer.Exit:
    """Log a failure, say so on stderr, and build the exit with which the run must end.

    The message goes to stderr in the form ``program: what went wrong``, which each utility
    uses. Thus a caller that redirects stdout still sees it, and a caller that parses stdout
    never has to filter it out. It has no mark and no colour. The exit status shows that the
    run failed, and this is the reason why the status is classified (refer to
    :mod:`meshterm.core.exitcodes`).

    MeshTerm logs it at the level warning and not error, because these are the ordinary
    failures, for example an absent device or a wrong password. A log that holds only
    crashes cannot answer the question "what happened just before it".

    Args:
        tool: The tool that failed, for the log line.
        problem: The exception or the message to report.
        code: The status to exit with.

    Returns:
        The :class:`typer.Exit` that the caller must raise.
    """
    get_logger().warning("%s failed: %s", tool.name, problem)
    script.stderr_console().print(f"meshterm: {problem}", style="err", highlight=False)
    return typer.Exit(code)


async def _drive(coro, ctx: AppContext):  # noqa: ANN201 - passes the awaited value through
    """Await a coroutine, then disconnect the device (but keep the repo open).

    Args:
        coro: The coroutine to run (a tool execution or the menu loop).
        ctx: The application context whose device must be closed afterward.

    Returns:
        The value that ``coro`` returned. On the scripted path, this is a
        :class:`~meshterm.tools.base.ToolResult`, and the caller changes its ``exit_code``
        to the status of the process.
    """
    try:
        return await coro
    finally:
        if ctx._device is not None:
            try:
                await ctx._device.disconnect()
            except Exception:  # noqa: BLE001 - a dead or lost link must not crash the teardown
                pass
            ctx._device = None


async def _execute_and_render(tool: Tool, params: dict, ctx: AppContext) -> ToolResult:
    """Run a tool, render the answer that it stated, and close with what it has to say.

    This is the only place where a report becomes output. The tool states what happened as
    data, and this function gives the data to the renderer that ``--json`` selected. The
    ``exit_code`` works in the same way: the tool states it here, and the caller changes it
    to the status of the process. Thus a third format is a new renderer, and it is not an
    edit to twenty tools.

    The ``message`` of a tool ("✓ applied 3 changes", "✓ traced Alice") goes to **stderr**,
    as each other acknowledgement does (refer to :meth:`~meshterm.ui.surface.PlainUi.ack`).
    It is the closing line and not the answer, so it must stay out of a redirect. It is also
    the count that a person at a prompt wanted after a command that scrolled. MeshTerm
    once dropped it. That threw it away to protect a stream that it never reached.

    Args:
        tool: The tool to run.
        params: The parameters for the tool.
        ctx: The application context.

    Returns:
        The :class:`ToolResult` of the tool.
    """
    result = await tool.execute(ctx, params)
    renderers.for_format(ctx.output, ctx.console).render(result.report)
    if result.message:
        ctx.ui.ack(result.message)
    return result


def _register_all() -> None:
    """Register the CLI subcommand of each tool with the Typer app."""
    for tool in all_tools():
        tool.register_cli(app)


_register_all()


def _owns_its_console() -> bool:
    """Whether this process is the only one on its console, that is, the user double-clicked it.

    Windows gives a console to a process that Explorer starts, and destroys the console when
    that process ends. Thus the console shows an error message and erases it at the same
    instant. If a shell starts the process, the console belongs to the shell and stays.

    ``GetConsoleProcessList`` shows the difference: it reports how many processes are
    attached to this console. If the count is one, MeshTerm is alone, and it holds a window
    that dies with it.

    Returns:
        ``True`` only on Windows, and only when no other process shares the console.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        buffer = (ctypes.c_uint * 4)()
        count = win32dll.kernel32().GetConsoleProcessList(buffer, 4)
    except Exception:  # pragma: no cover - there is no console at all, or kernel32 is a stub
        return False
    return count == 1


def main() -> None:
    """Console-script entry point.

    After a failure, the function keeps the window open when MeshTerm owns it. If it did
    not, the console would draw the error and destroy it together, and the person who must
    read the error would see only a flash. This happens only on that path. A successful run
    must not make anyone press a key, and a run from a shell leaves its output on the screen
    in any case.
    """
    try:
        # No wildcard expansion on Windows. Click expands an argument that has `*` to the
        # files that match it. It does this also when the shell passed the argument
        # through in quotes, because Click cannot see the difference. Thus
        # `chat send --scope '*'` arrived as the file names of the working directory. No
        # argument of MeshTerm is a file glob, and `*` is the wildcard region.
        app(windows_expand_args=False)
    except SystemExit as exit_request:
        if exit_request.code and _owns_its_console():
            _wait_before_the_window_closes()
        raise
    except BaseException:
        if _owns_its_console():
            _wait_before_the_window_closes()
        raise


def _wait_before_the_window_closes() -> None:
    """Ask for a key press so that a message stays readable. This function never fails."""
    try:
        print()
        print("-- Press Enter to close this window --")
        input()
    except Exception:  # pragma: no cover - stdin is closed, redirected, or gone
        pass


if __name__ == "__main__":
    main()
