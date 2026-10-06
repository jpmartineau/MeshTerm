# SPDX-License-Identifier: Apache-2.0
"""The ``diagnostics`` tool: all that is necessary for a bug report, in one block to paste.

If a report starts with "it crashed", two or three questions and answers are necessary
before anyone knows which MeshTerm, which OS, which terminal, and which radio. The person
who found the bug is usually the person who can answer least, because MeshTerm finds half
of these facts at boot and never shows them. This tool states all of them at one time.

**What it does not say, on purpose.** The block is made for a person who pastes it in
public and did not read it. Thus it must not contain anything that identifies a person or
unlocks anything: no pairing PIN, no admin password, no channel secret, no private key, no
position, and no name or key of a contact. The block describes the mesh only in
**totals**: how many rows of each type, and the span of time that they cover. That half
explains a bug (a database with four observations and one with four hundred thousand fail
in different ways), and it names nobody that the reporter talks to.
:func:`~meshterm.core.hostinfo.host` and
:meth:`~meshterm.persistence.repository.Repository.table_counts` keep that limit on their
own side. Thus this module cannot go past it by accident.

**One report, three faces.** That is the only reason why this is a report and not a
``print``. The menu draws it as a written page (refer to :mod:`meshterm.ui.diagnostics`)
and saves it as markdown. ``meshterm diagnostics`` prints the same facts as aligned
records, for a person who wants to redirect them and not read them. ``--json`` gives
them to the program that files the issue. The document face is the face that leaves this
machine. Thus it is written in a format that an issue tracker already renders.

The tool asks the device, but the device is not necessary. A report about a radio that
does not connect is the report that it is most necessary to file. Thus, when the tool
cannot get to the device, that failure becomes a fact in the block (``connected`` and
``error``), and not an error that replaces the block.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..context import AppContext
from ..core.models import node_type_label
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Report

#: The name of the saved file, in the config directory, next to the log that it goes with
#: in a bug report. The name includes the app name, and is not only ``diagnostics.md``.
#: The reason: the only thing that occurs to this file is that a person moves it to a
#: different place and attaches it. Then the directory that it came from no longer tells
#: what it is.
DIAGNOSTICS_FILENAME = "meshterm-diagnostics.md"


@register
class DiagnosticsTool(Tool):
    """What MeshTerm, this machine, this terminal, and this radio are."""

    name = "diagnostics"
    title = "Diagnostics"
    icon = "🩺"
    help = "Version, host, terminal, and device facts for a bug report"
    category = "This app"
    order = 7  # after Preferences (the row that changes MeshTerm), and before the pages

    def register_cli(self, app: Any) -> None:
        """Register the command, with the one option that changes the answer into a file.

        ``--out`` has the same spelling as ``config export-key --out``. We did not make a
        second word to write an answer to a path. Plain redirection continues to work, and
        the option does not replace it. The option adds a file whose columns have the full
        width, and not the width that the terminal had at that time. That is the
        difference between an attachment that a person can read and a folded one.
        """
        import typer

        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _command(
            out: Path | None = typer.Option(
                None, "--out", help="Write the block to this file instead of printing it"
            ),
        ) -> None:
            run_tool_command(self, {"out": out})

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the page in the menu. The return value ``None`` ends with no run row.

        The About pages use the same shape, and the same screen, because this page is a
        written page too. In the menu, the user only reads it. If a run row stores that a
        person looked at their own version number, the log gets a line for each look. A
        save from the page is also not a run.

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None`` in the menu. The CLI never gets to this method.
        """
        from ..ui.diagnostics import open_diagnostics_page

        report = await build_report(ctx)
        await open_diagnostics_page(ctx, markdown_source(report), save_path=default_path(ctx))
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """State the diagnostics, or write them to a file. Only the CLI gets to this method.

        With ``--out``, the answer *is the path*. ``config export-key`` makes the same
        choice. A caller who asked for a file wants to know where it is. The caller does
        not want to get again the contents that it sent into that file.

        Args:
            ctx: The shared application context.
            params: ``out``: a destination path, or ``None`` to print the block.

        Returns:
            A :class:`ToolResult` that holds the block, or the path of the written file.
        """
        report = await build_report(ctx)
        out = params.get("out")
        if out is None:
            return ToolResult(summary={"tool": self.name}, report=report)

        written = write_markdown(markdown_source(report), out)
        ctx.ui.ack("[ok]✓[/ok] diagnostics written — attach it to the report.")
        return ToolResult(
            summary={"tool": self.name, "path": str(written)},
            artifacts=[str(written)],
            report=(_written_to(written),),
        )


async def build_report(ctx: AppContext) -> Report:
    """Collect all the diagnostic facts, as the report that the two faces render.

    Seven blocks, in the order that is most useful to the user: what this program is, the
    machine it runs on, the terminal it draws into, the radio it found, how much history it
    has, what that history contains, and what is changed from the defaults. Each block has
    a ``caption``. The markdown face changes the captions into the ``##`` landmarks of the
    page.

    The five :class:`~meshterm.ui.report.Facts` blocks merge into one flat JSON object.
    The two :class:`~meshterm.ui.report.Listing` blocks each keep their own key. That is
    also why the table counts are a listing: if they were facts, a table with the same name
    as a fact could collide with it.

    Args:
        ctx: The shared application context.

    Returns:
        The report of seven blocks.
    """
    return (
        _app_facts(),
        _host_facts(),
        _terminal_facts(),
        await _device_facts(ctx),
        _mesh_facts(ctx),
        _table_listing(ctx),
        _preference_listing(ctx),
    )


def markdown_source(report: Report, *, when: datetime | None = None) -> str:
    """The full block as a markdown document: the source of the page, and the file contents.

    It is markdown because the two places where this text goes already use markdown. The
    menu draws it through :func:`~meshterm.ui.markdown.render_markdown`, the same renderer
    as the About pages. Thus the page gets real ``##`` landmarks, which pin while the page
    scrolls and which the section jumps go to. An issue tracker renders it as it is
    written, with no fence and no apology. The command line keeps its aligned records,
    because that face is a stream that a person searches with grep, and headings only add
    clutter to it.

    Args:
        report: The blocks to write.
        when: The time to put in the colophon (the default is now, in local time). A
            caller can give it, so that the same report can be rendered two times and
            compared.

    Returns:
        The markdown source, ready for :func:`~meshterm.ui.markdown.render_markdown` or
        for a file.
    """
    from .. import __version__
    from ..ui.renderers import markdown_blocks

    stamp = (when or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    return (
        # The `#` title holds what the frame around it cannot hold. For the same reason,
        # `about.md` has one and the other three written pages do not. The screen already
        # has the heading "Diagnostics". If the title repeats it, the first line of the
        # page says nothing. But the version must be at the top of a file that a
        # person will open later. The emphasis makes the version the muted qualifier next
        # to the name.
        f"# MeshTerm diagnostics *v{__version__}*\n\n"
        # The standfirst is flush and muted under the title. It says the one thing that the
        # user must know before the user reads the rest: it is safe to give this text to
        # other persons.
        "Everything a bug report opens with. Nothing here is private.\n\n"
        f"{markdown_blocks(report)}\n\n"
        "---\n\n"
        f"Taken {stamp}.\n"
    )


def default_path(ctx: AppContext) -> Path:
    """Where the page saves when no place is given: next to the log, in the config directory.

    It is the config directory, not the working directory. In the menu, the working
    directory is the directory from which the user started MeshTerm, and nobody can tell
    the user where that is. The log is also in the config directory, so "send me both"
    names one folder.
    """
    return ctx.settings.config_dir / DIAGNOSTICS_FILENAME


def write_markdown(source: str, path: Path | str) -> Path:
    """Write a markdown document to ``path``, and make its directory.

    **Always markdown**, whatever face the run printed. This file is for an attachment to
    an issue, where people read it, and an issue tracker renders markdown. A person who
    wants the machine face can redirect ``--json``, and already has a better name for that
    file than this one.

    Args:
        source: The document, from :func:`markdown_source`.
        path: The destination.

    Returns:
        The path of the written file.

    Raises:
        OSError: If the directory cannot be made, or if the file cannot be written.
    """
    written = Path(path)
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(source, encoding="utf-8")
    return written


def _written_to(written: Path) -> Any:
    """The answer of a ``--out`` run: only the path, on the plain face."""
    from ..ui import fields
    from ..ui.report import BARE, Facts

    return Facts(
        key="saved",
        fields=(fields.path("path", "path"),),
        values={"path": written},
        shape=BARE,
        bare="path",
    )


def _app_facts() -> Any:
    """What this program is: the version, how it was installed, and where it keeps data."""
    from .. import __version__
    from ..core import hostinfo
    from ..ui import fields
    from ..ui.report import Facts

    return Facts(
        key="meshterm",
        caption="Build",
        fields=(
            fields.word("meshterm", "meshterm"),
            fields.word("install", "install"),
        ),
        values={"meshterm": __version__, "install": hostinfo.install_kind()},
    )


def _host_facts() -> Any:
    """The machine below MeshTerm: which OS, which build, which processor, which Python."""
    from ..core import hostinfo
    from ..ui import fields
    from ..ui.report import Facts

    machine = hostinfo.host()
    return Facts(
        key="host",
        caption="Host",
        fields=(
            fields.word("os", "os"),
            fields.word("os_build", "os_build"),
            fields.word("arch", "arch"),
            fields.word("python", "python"),
        ),
        values={
            "os": machine.os,
            "os_build": machine.os_build,
            "arch": machine.arch,
            "python": machine.python,
        },
    )


def _terminal_facts() -> Any:
    """The terminal, and each decision about it that MeshTerm made one time at boot.

    These facts have their own section, and are not more host facts. The reason: a report
    of "it looks wrong" is about these facts. A user who looks quickly for them must not
    have to read the processor architecture first. Whether icons draw, whether the
    separators of the path widget draw, which platform flavour resolved, and *why*:
    MeshTerm decides each of these from the environment before the first frame, and no
    screen shows them.
    """
    from ..core import hostinfo
    from ..platforms import get_platform
    from ..ui import fields
    from ..ui.report import Facts
    from ..ui.termfont import detect_terminal_font, emoji_support, powerline_support

    term = hostinfo.terminal()
    emoji = emoji_support()
    powerline = powerline_support()
    font = detect_terminal_font()
    return Facts(
        key="terminal",
        caption="Terminal",
        fields=(
            fields.word("terminal", "terminal"),
            fields.word("term", "term"),
            fields.word("colorterm", "colorterm"),
            fields.word("terminal_size", "terminal_size"),
            fields.flag("over_ssh", "over_ssh"),
            fields.word("platform", "platform"),
            fields.flag("icons", "icons"),
            fields.word("icons_source", "icons_source"),
            fields.word("font", "font"),
            fields.word("powerline", "powerline"),
        ),
        values={
            "terminal": term.program,
            "term": term.term,
            "colorterm": term.colorterm,
            "terminal_size": f"{term.cols}x{term.rows}",
            "over_ssh": term.over_ssh,
            "platform": get_platform().name,
            "icons": emoji.supported,
            "icons_source": emoji.source,
            # The font face, and the terminal from which it was read. A terminal that is not
            # identified (or an ssh session, where the font is on the far client) has no
            # answer. To say so is better than to name a font that nobody here can see.
            "font": f"{font.face} ({font.source})" if font else None,
            # The level and the source together. `none (font:windows-terminal)` is a
            # measurement and `unknown (ssh)` says that MeshTerm does not know. They lead
            # to different places.
            "powerline": f"{powerline.level} ({powerline.source})",
        },
    )


async def _device_facts(ctx: AppContext) -> Any:
    """The radio: what it is, how it is connected, and the settings of its link.

    This function does what it can, on purpose. Each value here is ``None`` on a machine
    whose radio is disconnected, asleep, or does not pair. For that bug report,
    ``connected: false`` with the words of the failure in ``error`` is a better first line
    than a command that did not make a report.
    """
    from ..ui import fields
    from ..ui.report import Facts

    values: dict[str, Any] = dict.fromkeys(
        (
            "connected",
            "transport",
            "port",
            "device_role",
            "device_model",
            "firmware",
            "radio_freq_mhz",
            "radio_bw_khz",
            "radio_sf",
            "radio_cr",
            "tx_power_dbm",
            "error",
        )
    )
    values["connected"] = False
    try:
        device = await ctx.device()
        snapshot = await _snapshot(ctx, device)
        info = await _device_info(device)
        adv_type = snapshot.get("adv_type")
        values.update(
            connected=True,
            transport="mock" if ctx.mock else (ctx.active_transport or "serial"),
            port=ctx.active_port,
            device_role=node_type_label(adv_type),
            device_model=info.get("model") or None,
            firmware=" ".join(str(info[k]) for k in ("ver", "fw_build") if info.get(k)) or None,
            radio_freq_mhz=snapshot.get("radio_freq"),
            radio_bw_khz=snapshot.get("radio_bw"),
            radio_sf=snapshot.get("radio_sf"),
            radio_cr=snapshot.get("radio_cr"),
            tx_power_dbm=snapshot.get("tx_power"),
        )
    except Exception as exc:  # noqa: BLE001 - each failure to get to the radio is the fact
        values["error"] = str(exc) or exc.__class__.__name__

    return Facts(
        key="device",
        caption="Radio",
        fields=(
            fields.flag("connected", "connected"),
            fields.word("transport", "transport"),
            fields.word("port", "port"),
            fields.word("device_role", "device_role"),
            fields.word("device_model", "device_model"),
            fields.word("firmware", "firmware"),
            fields.decimal("radio_freq_mhz", "radio_freq_mhz", ".4f"),
            fields.decimal("radio_bw_khz", "radio_bw_khz", ".2f"),
            fields.integer("radio_sf", "radio_sf"),
            fields.integer("radio_cr", "radio_cr"),
            fields.integer("tx_power_dbm", "tx_power_dbm"),
            fields.free("error", "error"),
        ),
        values=values,
    )


def _mesh_facts(ctx: AppContext) -> Any:
    """How much history this installation holds, and where it holds it.

    Counts and a span, never a row. The span gives the scale of each other number here.
    A thousand observations in two years and a thousand in an afternoon describe two
    different installations, and only one of them has a radio that is possibly faulty.
    """
    from ..core.preferences import current
    from ..persistence.logging import log_path
    from ..ui import fields
    from ..ui.report import Facts

    preferences = ctx.preferences or current()
    db_path = ctx.settings.db_path
    first, last = ctx.repo.observation_span()
    return Facts(
        key="mesh",
        caption="Storage",
        fields=(
            fields.path("config_dir", "config_dir"),
            fields.integer("db_size_kb", "db_size_kb"),
            fields.word("log_level", "log_level"),
            fields.integer("log_size_kb", "log_size_kb"),
            fields.integer("runs_failed", "runs_failed"),
            fields.instant("first_heard", "first_heard"),
            fields.instant("last_heard", "last_heard"),
        ),
        values={
            "config_dir": ctx.settings.config_dir,
            "db_size_kb": _size_kb(db_path),
            "log_level": preferences.log_level,
            # How much the log holds. Thus a maintainer who asks for it knows what to expect.
            # Also, a file that is still at zero shows that the level never let it write.
            "log_size_kb": _size_kb(log_path(ctx.settings.config_dir)),
            "runs_failed": ctx.repo.failed_run_count(),
            "first_heard": first,
            "last_heard": last,
        },
    )


def _table_listing(ctx: AppContext) -> Any:
    """Each table in the database, and how many rows it holds.

    A listing, and not more facts, for two reasons. First, the keys come from the schema,
    and are not declared. Thus, if they were facts, a table with the same name as a fact
    could collide with it in the merged JSON object. Second, this is in fact a set of
    records (one for each table), and a person and a parser both already know that shape.
    """
    from ..ui import fields
    from ..ui.report import Column, Listing

    counts = ctx.repo.table_counts()
    return Listing(
        key="tables",
        caption="Stored rows",
        columns=(fields.word("table", "TABLE"), Column(key="rows", lanes=(_rows_lane(),))),
        rows=[{"table": name, "rows": n} for name, n in counts.items()],
    )


def _preference_listing(ctx: AppContext) -> Any:
    """The preferences that are not at their defaults: all of them, and only those.

    An override is the only part of the preferences that can explain something. The
    defaults are in the source, and they are the same for all users. If the list shows
    all forty preferences, the two that the reporter changed are hard to find.
    """
    from ..core.preferences import current
    from ..ui import fields
    from ..ui.report import Listing

    preferences = ctx.preferences or current()
    return Listing(
        key="preferences",
        caption="Changed preferences",
        columns=(fields.word("preference", "PREFERENCE"), fields.free("value", "VALUE")),
        rows=[
            {"preference": key, "value": str(value)}
            for key, value in preferences.overrides().items()
        ],
    )


def _rows_lane() -> Any:
    """The lane of row counts: right-aligned, because only aligned numbers are easy to compare."""
    from ..ui.report import Lane

    return Lane(header="ROWS", render=lambda v: str(v if v is not None else 0), align="right")


def _size_kb(path: Any) -> int | None:
    """The size of a file in whole kilobytes, or ``None`` when there is no file to measure."""
    if path is None:
        return None
    try:
        return round(path.stat().st_size / 1024)
    except OSError:
        return None


async def _snapshot(ctx: AppContext, device: Any) -> dict:
    """The snapshot of the device settings, or an empty snapshot when the read fails."""
    from ..ui.config_editor import cached_snapshot

    try:
        return await cached_snapshot(ctx, device)
    except Exception:  # noqa: BLE001 - if the settings read fails, the radio facts stay blank
        return {}


async def _device_info(device: Any) -> dict:
    """The self-description of the device, or an empty one when the firmware does not answer."""
    try:
        return await device.get_device_info() or {}
    except Exception:  # noqa: BLE001 - firmware older than the query gives nothing
        return {}
