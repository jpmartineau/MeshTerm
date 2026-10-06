# SPDX-License-Identifier: Apache-2.0
"""The trace tools: path traces, live in the menu, and a single run on the CLI.

A trace is one walked path. The protocol has no destination field, so "target" is only
a UX concept. The menu divides the feature at exactly that line:

* **Trace target** (``trace``): *can I get to this node?* Select a target from a list in
  the order of recency. The live screen composes and explores symmetric routes that turn
  at the target and come back over the mirrored hops. The list stays pushed below for
  the full visit. Thus Esc from a walk goes back to the row that started it (sorted
  again by the last trace), and the next node is one Enter away.
* **Trace path** (``trace-path``): *how far can a route that I make go?* No target and
  no picker. The user composes the full walk hop by hop, and it must only end at a node
  that our node can hear. The traces are stored under the ``(path)`` sentinel, so the
  composed walks stay out of the history of the target picker.

Both tools open the live trace screen (:mod:`meshterm.ui.trace_screen`) ready but idle.
Nothing transmits until the user starts Trace, which runs the selected sample count
(1–8 traces, with a pause between transmissions). Repeaters penalize, and can
blacklist, nodes that transmit in bursts. Thus a run of more than one trace always
waits a cooldown between sends. On the CLI, each tool stays a scriptable single run:
run one trace, print the route and the SNR of each hop, and exit.

The two front ends store the same data: one ``runs`` row for each trace, with the trace
stored under it. Thus the stored history is the same, wherever it came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.models import (
    LOCAL_DEVICE_LABEL,
    PATH_TRACE_TARGET,
    Contact,
    TraceResult,
    TraceStats,
)
from ..services import trace_runner
from ..ui.widgets import stats_panel
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.contactlist import ContactListScreen, ContactRow

#: The characters that a stored trace target must contain, and only these, to be a hex key
#: prefix when the target picker folds it back to a contact name.
_HEX_DIGITS = frozenset("0123456789abcdef")


@register
class TraceTool(Tool):
    """Trace the path to a target and watch the SNR of each hop, live or scripted."""

    name = "trace"
    title = "Trace target"
    icon = "🎯"
    help = "Trace a target and watch per-hop SNR"
    category = "Explore"
    order = 30  # find a node with the tool above, and walk to it here

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Nothing to collect here. The target picker is in :meth:`run`.

        The selection and the screen that it opens share the read of the contacts and the
        routing width. Also, the live path does not use the run row of the base class
        (refer to :meth:`execute`). Thus the full entry flow is in :meth:`_run_live`, and
        not half of it here.

        Args:
            ctx: The shared application context.

        Returns:
            ``{"live": True}``: the marker of the menu for the interactive path.
        """
        return {"live": True}

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run, with no outer run row on the live path.

        The live screen opens one ``runs`` row *for each trace* (the same row that a
        scripted run stores). If another row goes around the full screen session, the log
        has each trace two times. Scripted runs keep the logging of the base class.

        Args:
            ctx: The shared application context.
            params: The parameters for this run.

        Returns:
            The :class:`ToolResult` from :meth:`run`.
        """
        if params.get("live"):
            return await self.run(ctx, params)
        return await super().execute(ctx, params)

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Open the live screen (menu), or run one stored trace (CLI).

        Args:
            ctx: The shared application context.
            params: ``live`` from the menu (the user selects the target on the screen).
                Or, from the CLI: ``target`` (str), the optional ``path``, and the
                injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the result of the trace.
        """
        if params.get("live"):
            return await self._run_live(ctx)
        return await self._run_cli(ctx, params)

    # -- interactive (menu) -------------------------------------------------------

    async def _run_live(self, ctx: AppContext) -> ToolResult:
        """Ask for the target in a dialog, then run the live screen over the menu.

        The picker is a *question*, not a place. It is a floating list over the menu, and
        it closes immediately when the user selects a target. Thus the trace screen opens
        directly over the menu, and Esc from it goes back there. Before, the picker stayed
        pushed as a hub under the trace screen, and Esc from a trace went back to the
        list. That looked like two places, but the trace is the full visit. To trace
        another node, the user opens the tool again.

        Args:
            ctx: The shared application context.

        Returns:
            A :class:`ToolResult` that counts the opened screen and the traces that ran.
        """
        from ..ui.trace_screen import open_trace
        from ..ui.tui.screen import CANCEL

        picker = await self._build_picker(ctx)
        if picker is None:
            # Nothing to list (or no full-screen session): the user types the target.
            target = await self._prompt_target(ctx)
        else:
            chosen = await ctx.ui.session.run_dialog(picker)
            target = None if chosen is CANCEL or chosen is None else chosen.name
        if target is None:
            return ToolResult(summary={})
        return ToolResult(summary={"sessions": 1, "traces": await open_trace(ctx, target)})

    async def _prompt_target(self, ctx: AppContext) -> str | None:
        """Ask the user to type a target: the way in when there is no list to select from.

        MeshTerm comes here when there are no known contacts (an empty list gives nothing
        to select), and on each surface that has no full-screen session. It is also the
        only way to trace a bare key prefix of a node that the device does not have as a
        contact. It is a question, the same as the list that it replaces. Thus it floats
        over the menu, and does not fill the frame.

        Args:
            ctx: The shared application context.

        Returns:
            The typed target, or ``None`` if the user left it blank or cancelled it.
        """
        entered = await ctx.ui.text("Target node (name or key prefix):", floating=True)
        return entered.strip() if entered else None

    async def _build_picker(self, ctx: AppContext) -> ContactListScreen | None:
        """Make the trace target picker, or return ``None`` when there is no list to draw.

        It has the same ``NAME · TRACED · HEARD · PKTS · KEY`` lanes, Ctrl+arrow sort ring,
        and type-to-filter as the Contacts screen and the courier recipient. But it lists
        all the node types (a trace answers *can I get to this node?* for a repeater or a
        room, the same as for a companion). It also has one more lane, ``TRACED``: how
        long ago each node was last traced. The list opens sorted by ``TRACED``, in
        descending order. Thus the most recently traced node comes first, and the nodes
        that were never traced are at the bottom. Enter commits the highlighted node as
        the target.

        Args:
            ctx: The shared application context.

        Returns:
            The picker, or ``None`` when there is nothing to list (refer to
            :meth:`_prompt_target`).
        """
        from ..ui.contactlist import (
            TRACE_LANES,
            TRACE_SORT_COLUMNS,
            TRACE_SORT_OPENS_ASCENDING,
            ContactListScreen,
        )
        from ..ui.surface import TuiUi
        from ..ui.widgets import ContactsSort

        # Read through the session cache. This picker runs each time that Trace opens, and
        # the contacts table is a slow round trip on a busy node. When MeshTerm read it
        # again here (before the trace screen, which is already cached), Trace seemed to
        # stop each time it opened. Refer to
        # :class:`~meshterm.services.device_state.DeviceState`.
        contacts = await ctx.devstate.contacts()
        if not contacts or not isinstance(ctx.ui, TuiUi):
            return None

        picker = ContactListScreen(
            "Trace target — pick a target",
            rows=self._picker_rows(ctx, contacts),
            prefix_bytes=await ctx.devstate.routing_prefix_bytes(),
            sort=ContactsSort.from_name("traced", TRACE_SORT_COLUMNS, TRACE_SORT_OPENS_ASCENDING),
            footer_hint="↑↓ move · ^←→↑↓ sort · type to filter · Enter select · Esc back",
            lanes=TRACE_LANES,
        )
        # In all other places, the contact list is a full-screen page. Here it is a
        # question before the screen opens, so it floats over the menu, the same as each
        # other selection before a tool.
        picker.floating = True
        return picker

    @staticmethod
    def _picker_rows(ctx: AppContext, contacts: list[Contact]) -> list[ContactRow]:
        """The lane data of the picker for ``contacts``, read again from the stored history.

        The ``TRACED`` ages and the packet counts both come from the repository. Thus it
        costs little to do this again each time that a trace returns to the picker. That
        keeps the lane by which the list is *sorted* correct for the last walk.

        Args:
            ctx: The shared application context.
            contacts: The contacts of the device, as listed.

        Returns:
            One :class:`~meshterm.ui.contactlist.ContactRow` for each contact, in the given
            order (the screen sorts them).
        """
        from ..ui.contactlist import ContactRow
        from ..ui.widgets import contact_packets

        traced = _last_traced_by_name(contacts, ctx.repo.target_last_traced())
        counts = {n.node: n.count for n in ctx.repo.heard_nodes() if n.node}
        return [
            ContactRow(
                value=c,
                name=c.name,
                key=c.public_key or c.key_prefix or "",
                node_type=c.node_type,
                last_seen=c.last_seen,
                count=contact_packets(c, counts),
                last_traced=traced.get(c.name),
            )
            for c in contacts
        ]

    # -- scripted (CLI) -------------------------------------------------------------

    async def _run_cli(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run one trace, store it, and print the route and the summary of each hop.

        On the CLI, one transmission for each run is a strict rule, because repeaters
        penalize (and can blacklist) nodes that transmit in bursts. Thus, to take more
        samples in a script, run the command again (with your own pause between runs).

        Args:
            ctx: The shared application context.
            params: ``target`` (str), the optional ``path``, and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the result of the trace.
        """
        return await _trace_once_cli(
            ctx, params["_run_id"], target=params["target"], path_spec=params.get("path")
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``trace`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(
            name=self.name, help="Run a single path trace to a target and show per-hop SNR"
        )
        def _trace(
            target: str = typer.Option(..., "--target", "-t", help="Target node name/prefix"),
            path: str | None = typer.Option(
                None,
                "--path",
                # No ``-p`` short form. ``-p`` is the global ``--profile``, and
                # ``_globals_first`` moves a group option in front of the subcommand, at any
                # position where it is typed. Thus a leaf ``-p`` can never get to this option.
                # It can only hide it.
                help="Force a route: comma-separated contact names/hex prefixes (e.g. 3d,f2,3d)",
            ),
        ) -> None:
            tool_params: dict[str, Any] = {"target": target}
            if path:
                tool_params["path"] = path
            run_tool_command(self, tool_params)


@register
class TracePathTool(Tool):
    """Walk a route that you compose by hand (no target), and see how far it goes."""

    name = "trace-path"
    title = "Trace path"
    icon = "👣"
    help = "Walk a route you compose, hop by hop"
    category = "Explore"
    order = 32

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """No parameters to collect, because a path walk has no target to select.

        Args:
            ctx: The shared application context.

        Returns:
            ``{"live": True}``. The user composes the route itself on the screen.
        """
        return {"live": True}

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run, with no outer run row on the live path (refer to :class:`TraceTool`).

        Args:
            ctx: The shared application context.
            params: The parameters for this run.

        Returns:
            The :class:`ToolResult` from :meth:`run`.
        """
        if params.get("live"):
            return await self.run(ctx, params)
        return await super().execute(ctx, params)

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Open the live path walk screen (menu), or run one stored trace (CLI).

        Args:
            ctx: The shared application context.
            params: ``live`` from the menu. Or, from the CLI: ``path`` (str, necessary)
                and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the result of the walk.
        """
        if params.get("live"):
            from ..ui.trace_screen import open_trace_path

            traces = await open_trace_path(ctx)
            return ToolResult(summary={"traces": traces})
        return await _trace_once_cli(
            ctx, params["_run_id"], target=PATH_TRACE_TARGET, path_spec=params["path"]
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``trace-path`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(
            name=self.name,
            help="Walk a composed route once (no target) and show per-hop SNR",
        )
        def _trace_path(
            path: str = typer.Option(
                ...,
                "--path",
                # No ``-p`` short form. ``-p`` is the global ``--profile``, and
                # ``_globals_first`` moves a group option in front of the subcommand, at any
                # position where it is typed. Thus a leaf ``-p`` can never get to this option.
                # It can only hide it.
                help="The whole walk: comma-separated contact names/hex prefixes "
                "(must end within earshot of this node)",
            ),
        ) -> None:
            run_tool_command(self, {"path": path})


async def _trace_once_cli(
    ctx: AppContext, run_id: int, *, target: str, path_spec: str | None
) -> ToolResult:
    """Run one stored trace, and print the route and the summary of each hop (CLI body).

    The two scripted trace commands share this function. ``trace`` gives its target (the
    path is optional, because the device can route without one). ``trace-path`` gives the
    :data:`~meshterm.core.models.PATH_TRACE_TARGET` sentinel and a necessary path.

    Args:
        ctx: The shared application context.
        run_id: The run row, already open, under which to store the trace.
        target: The label under which the trace is stored.
        path_spec: The forced route as typed (names or hex, with commas between them), if
            there is one.

    Returns:
        A :class:`ToolResult` with the result of the trace.
    """
    from ..ui.surface import TuiUi

    is_walk = target == PATH_TRACE_TARGET
    device = await ctx.device()

    # The name of our device is the label of the two ends of the path (the origin of the
    # first hop, and the destination of the last hop). If the name is not available, use a
    # neutral label.
    self_info = await device.get_self_info()
    device_label = str(self_info.get("name") or LOCAL_DEVICE_LABEL)
    # Our own public key. Thus the ends of the route (our node) have a hash, the same as
    # each other hop, at the same path hash width.
    device_hash = str(self_info.get("public_key") or "") or None

    # Resolve the repeater hashes in the results to contact names, when we know them. Thus
    # the tables show names, not hex prefixes that tell nothing.
    contacts = await device.get_contacts()
    resolve = trace_runner.make_node_resolver(contacts)

    # Resolve an optional forced path (names or hex) to the hex string that the radio
    # accepts. A blank path is fully supported for a *target* trace: the device uses again
    # the route that it already learned (or floods if it has no route). A path walk always
    # has a path.
    path: str | None = None
    if path_spec:
        path = trace_runner.parse_trace_path(path_spec, contacts)

    with ctx.ui.progress("trace") as progress:
        task = progress.add_task("walking the path" if is_walk else f"tracing {target}", total=1)
        result = await device.run_trace(target, path=path)
        ctx.repo.record_trace(run_id, result)
        progress.advance(task)

    stats = TraceStats.from_traces(target, [result])
    if isinstance(ctx.ui, TuiUi):
        # The stats panel renders the route and the readings of each hop for the single
        # trace (its medians are the same as the readings themselves).
        ctx.ui.show(
            stats_panel(
                stats,
                device_label,
                resolve,
                route=result if result.success else None,
                device_hash=device_hash,
            )
        )
    report = None
    if not isinstance(ctx.ui, TuiUi):
        report = _trace_report(result, target, path, device_label, resolve, device_hash)

    summary: dict[str, Any] = {
        "target": target,
        "success": result.success,
        "hops": result.hop_count if result.success else None,
        "min_snr": result.min_snr,
        "rtt_ms": result.round_trip_ms,
    }
    if path:
        summary["path"] = path
    via = f" via [brand]{path}[/brand]" if path else ""
    if is_walk:
        message = (
            f"[ok]✓[/ok] the path came home{via}"
            if result.success
            else f"[err]✗[/err] no reply{via}"
        )
    elif result.success:
        message = f"[ok]✓[/ok] traced [brand]{target}[/brand]{via}"
    else:
        message = f"[err]✗[/err] no reply from [brand]{target}[/brand]{via}"
    # A trace that never came back is not a failure of the command: the radio transmitted,
    # and the walk ran. It is a walk with nothing to report, which is what NO_RESULT says.
    # It is also the answer on which a script most often branches.
    return ToolResult(
        summary=summary,
        message=message,
        report=report,
        exit_code=exitcodes.OK if result.success else exitcodes.NO_RESULT,
    )


def _trace_report(
    result: TraceResult,
    target: str,
    path: str | None,
    device_label: str,
    resolve: Any,
    device_hash: str | None,
) -> tuple:
    """State one trace: the result of the walk, then the readings of each hop.

    Two blocks. The first block is what the walk *did*: one fact on each line. ``route``
    is one of these facts, as a drawn line, and each node in it has a name and the hash by
    which it was addressed. The second block has a record for each hop. It names the two
    ends of a hop by that same **hash**, and does not repeat the names. The names are in
    the route line above, and the hash joins the two blocks (here, the hash shows the
    identity of a node, as its colour does on a screen). The document puts the full node
    at the two ends instead, because a structural join does not use a key, and each edge
    must be readable alone.

    Our node is a hop like any other, with a name and a hash. The menu draws it as ``★``,
    because the user never has to be told which node is theirs. But this line is often
    read from a file by a person who was not at the prompt when it ran.

    Args:
        result: The trace that ran.
        target: The label to which it was addressed.
        path: The forced route as hex, or ``None`` when the device routed it.
        device_label: The name of our node, at the two ends of the walk.
        resolve: Maps a hop hash to a friendly name, when the name is known.
        device_hash: Our own public key, so that our ends have a hash, the same as each
            other hop.

    Returns:
        The blocks of the report.
    """
    from ..ui import fields
    from ..ui.fields import NodeRef
    from ..ui.report import Facts, Listing

    hash_bytes = result.path_hash_bytes

    def short(value: str | None) -> str | None:
        """A hash, at the width with which this trace addressed the nodes."""
        if not value:
            return None
        raw = value.lower().removeprefix("0x")
        return raw[: hash_bytes * 2] if hash_bytes else raw

    def node(label: str) -> NodeRef:
        """One end of a hop, in the shared node shape."""
        if not label or label == device_label:
            return NodeRef(name=device_label, hash=short(device_hash), is_self=True)
        return NodeRef(name=resolve(label) or None, hash=short(label))

    edges = result.edges(device_label)
    route = None
    if edges:
        route = [node(edges[0].origin)] + [node(edge.destination) for edge in edges]

    facts = Facts(
        key="trace",
        fields=(
            fields.word("target", "target"),
            fields.spec(),
            fields.flag("success", "success"),
            fields.integer("hops", "hops"),
            fields.snr("min_snr_db", "min_snr_db"),
            fields.decimal("rtt_ms", "rtt_ms", ".0f"),
            # A new field on the machine face. The model always had the TX power at which
            # the walk ran, and the plain facts block has no space for it.
            fields.hidden("tx_dbm"),
            # What `hash` means on the nodes of this walk. Thus a consumer can join two
            # traces that addressed the same node at different widths.
            fields.hidden("hash_bytes"),
            fields.route("route", "route"),
        ),
        values={
            "target": target if target != PATH_TRACE_TARGET else None,
            "path": path,
            "success": result.success,
            "hops": result.hop_count if result.success else None,
            "min_snr_db": result.min_snr,
            "rtt_ms": result.round_trip_ms,
            "tx_dbm": result.tx_power,
            "hash_bytes": hash_bytes,
            "route": route,
        },
    )
    hops = Listing(
        key="edges",
        columns=(
            fields.integer("index", "HOP"),
            fields.node("from", lanes=(("hash", "FROM"),)),
            fields.node("to", lanes=(("hash", "TO"),)),
            fields.snr("snr_db", "SNR_DB"),
        ),
        rows=[
            {
                "index": edge.index,
                "from": node(edge.origin),
                "to": node(edge.destination),
                "snr_db": edge.snr,
            }
            for edge in edges
        ],
    )
    return (facts, hops)


def _last_traced_by_name(
    contacts: list[Contact], traced: dict[str, datetime]
) -> dict[str, datetime]:
    """Map the name of each contact to the time when that node was last traced.

    The ``TRACED`` lane and the default sort of the Trace picker read this map. The stored
    trace targets are filed exactly as the user addressed them: a contact name one day, a
    raw hex key prefix on another day. Thus each target is folded onto the contact that it
    names (by name, case-insensitive, or as a prefix of the public key of a contact). The
    *latest* trace time wins. Thus a node that was traced under the two spellings still
    shows one correct "last traced" age. This is the inverse of the old fold of recent
    targets in the picker, kept for each contact, not as a list of names.

    Args:
        contacts: The current contacts of the device.
        traced: ``target → last-traced time`` from
            :meth:`~meshterm.persistence.repository.Repository.target_last_traced`.

    Returns:
        ``contact name → last-traced time`` for each contact that the history can find.
    """
    by_fold = {c.name.casefold(): c.name for c in contacts}
    out: dict[str, datetime] = {}

    def note(name: str, when: datetime) -> None:
        current = out.get(name)
        if current is None or when > current:
            out[name] = when

    for target, when in traced.items():
        named = by_fold.get(target.casefold())
        if named is not None:
            note(named, when)
            continue
        needle = target.lower().removeprefix("0x")
        # Fold only possible key prefixes (≥2 bytes of hex). A short *name* that looks like
        # hex, such as "ace", must not be mistaken for an address.
        if len(needle) >= 4 and all(ch in _HEX_DIGITS for ch in needle):
            for contact in contacts:
                if (contact.public_key or "").lower().startswith(needle):
                    note(contact.name, when)
    return out
