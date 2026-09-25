# SPDX-License-Identifier: Apache-2.0
"""The ``monitor`` tool: a bounded foreground capture of what the mesh is saying.

Passive monitoring records every advert and telemetry frame the companion overhears —
with SNR, RSSI, and any shared location — to the database, building the longitudinal
history that the map's packet counts and the nodes list read back. It transmits nothing;
it only listens.

Recording is always on: the session-wide
:class:`~meshterm.services.event_hub.EventHub` overhears every packet, and
:class:`~meshterm.services.monitor_service.MonitorService` (``ctx.monitor``) writes them
to history as one of its subscribers, from the moment the radio opens. In the menu the
live packet counters show in the persistent header and the accumulated data surfaces
through Nodes and Map, so there is no separate screen here — this tool is CLI-only:
``meshterm monitor --seconds 60`` tails each overheard packet to the console and
summarizes the window when it ends.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.events import EventKind, MeshEvent
from ..core.models import NODE_TYPE_LABELS, HeardNode, Observation
from ..ui import renderers
from ..ui.fields import NodeRef, Position
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Listing


@register
class MonitorTool(Tool):
    """Capture overheard packets in the foreground for a bounded window (CLI only)."""

    name = "monitor"
    title = "Monitor"
    icon = "🎧"
    help = "Capture overheard packets live for a while and summarize them"
    category = "Watch"
    order = 50
    menu_visible = False  # recording is always on; in the menu the header/Nodes/Map show it

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run directly, without a logged ``runs`` row.

        The capture's observations are recorded under the monitor service's own
        ``monitor`` run, so wrapping this invocation in a second run row (as the base
        :meth:`Tool.execute` would) would double-log the session.

        Args:
            ctx: Shared application context.
            params: Parameters for this invocation.

        Returns:
            The :class:`ToolResult` from :meth:`run`.
        """
        return await self.run(ctx, params)

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Record overheard packets in the foreground for a bounded window.

        Connects the device, ensures history recording and the event hub are running,
        and tails each overheard packet to the console until the window ends (or the
        user interrupts). Observations are persisted by the monitor service exactly as
        in an interactive session; this adds a live console view and a window-scoped
        heard-node summary on top.

        Args:
            ctx: Shared application context.
            params: ``seconds`` — how long to capture (``0``/``None`` = until Ctrl-C).

        Returns:
            A :class:`ToolResult` with the window's packet and node counts.
        """
        from ..ui import script

        seconds = int(params.get("seconds") or 0)
        await ctx.device()  # surface connection problems before announcing the capture
        await ctx.monitor.start()  # register history recording before the hub pumps
        await ctx.events.start()
        # The announcement is about the run, not part of its answer, so it goes to stderr
        # and stays out of `meshterm monitor -s 60 > packets.txt`.
        script.stderr_console().print(
            "monitoring" + (f" for {seconds}s" if seconds else " — press Ctrl-C to stop"),
            style="muted",
            highlight=False,
        )
        seen: list[Observation] = []

        def on_observation(event: MeshEvent) -> None:
            obs = event.observation
            if obs is None:
                return
            seen.append(obs)
            emit(
                {
                    "observed_at": obs.observed_at,
                    "node": NodeRef(
                        name=obs.name,
                        key=(obs.public_key or "").lower() or None,
                        hash=obs.node,
                        type=NODE_TYPE_LABELS.get(obs.node_type),
                    ),
                    "kind": obs.kind,
                    "snr_db": obs.snr,
                    "rssi_dbm": obs.rssi,
                    "position": _position(obs.lat, obs.lon),
                    # The region a flood was sent into, resolved against the names known
                    # right now; a direct frame (or a class with no route) has none.
                    "scope": ctx.region_store.scope_of(obs.raw),
                    "path": _relays(obs.path),
                }
            )

        with renderers.stream(ctx, _live_packets()) as emit:
            unsubscribe = ctx.events.subscribe(on_observation, EventKind.OBSERVATION)
            try:
                if seconds:
                    await asyncio.sleep(seconds)
                else:
                    await asyncio.Event().wait()  # until Ctrl-C / cancellation
            except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover - interactive
                pass
            finally:
                unsubscribe()
                await ctx.monitor.stop()  # close the run row so the capture is a full record

        by_node: dict[str, list[Observation]] = {}
        for obs in seen:
            by_node.setdefault(obs.node, []).append(obs)
        nodes = [HeardNode.from_observations(node, group) for node, group in by_node.items()]
        nodes.sort(key=lambda n: n.last_seen, reverse=True)
        # The window is over, so its facts are worth saying — on stderr, where everything
        # about a run goes, keeping the packets a caller redirected the run for on their own.
        script.stderr_console().print(
            f"captured {len(seen)} packet{'' if len(seen) == 1 else 's'} "
            f"from {len(nodes)} node{'' if len(nodes) == 1 else 's'}"
            + (f" in {seconds}s" if seconds else ""),
            style="muted",
            highlight=False,
        )
        return ToolResult(
            summary={"seconds": seconds, "packets": len(seen), "nodes": len(nodes)},
            report=(_heard(nodes),),
            exit_code=exitcodes.OK if seen else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``monitor`` subcommand (the bounded foreground capture).

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _monitor(
            seconds: int = typer.Option(
                0, "--seconds", "-s", help="How long to capture (0 = until Ctrl-C)"
            ),
        ) -> None:
            run_tool_command(self, {"seconds": seconds})


def _position(lat: float | None, lon: float | None) -> Position | None:
    """A reception's shared position, or ``None`` where the packet carried none."""
    return Position(lat, lon) if lat is not None and lon is not None else None


def _relays(path: str | None) -> list[str] | None:
    """A packet's relay chain as hashes, in propagation order.

    Hashes rather than the shared node shape, and that is the honest answer: nothing
    resolves a relay to a node at reception time, so a node object per hop would be four
    ``null``s wrapped round a hash. ``[]`` is a direct reception (zero hops); ``None``
    means the packet class carries no path at all, which is a different fact.
    """
    if path is None:
        return None
    return [hop for hop in path.split(",") if hop]


def _live_packets() -> Listing:
    """The shape the live capture streams: one record per packet, as it lands.

    ``TIME`` is absolute where a listing's times are ages, and for the reason the whole
    rule turns on: every row of a live tail would read ``now``. ``SCOPE`` says which region
    a flood was sent into (:func:`~meshterm.ui.fields.scope`), ``null`` in the document for
    a frame that has none. The **name goes last**, so
    the one field with no width cannot push a lane — which is what finally let the two
    streams have columns at all, and with them the quoting could go.
    """
    from dataclasses import replace

    from ..ui import fields
    from ..ui.report import Listing

    def pin(column, width: int):  # noqa: ANN001, ANN202 - one column in, one column out
        return replace(column, lanes=tuple(replace(lane, width=width) for lane in column.lanes))

    node = fields.node("node", lanes=(("hash", "NODE"), ("name", "NAME")))
    node = replace(
        node,
        lanes=(replace(node.lanes[0], width=8), node.lanes[1]),
    )
    return Listing(
        key="observations",
        columns=(
            pin(fields.instant("observed_at", "TIME"), 25),
            node,
            # The packet class, which the plain stream has never had room for. New
            # surface: the simulator does not exercise every class, so a consumer should
            # treat an unfamiliar word as a word rather than an error.
            fields.hidden("kind"),
            pin(fields.snr("snr_db", "SNR_DB"), 6),
            pin(fields.decimal("rssi_dbm", "RSSI_DBM", ".0f"), 8),
            pin(fields.position(), 19),
            # The flood's scope — a region name, ``unknown`` or ``unscoped``, ``-`` for a
            # frame with none. Pinned to hold the two words whole; a long region name
            # overruns and pushes the row right, which is why NAME still goes last.
            pin(fields.scope(), 10),
            fields.hidden("path"),
        ),
        order=("TIME", "NODE", "SNR_DB", "RSSI_DBM", "LOCATION", "SCOPE", "NAME"),
    )


def _heard(heard: list[HeardNode]) -> Listing:
    """The capture window's per-node summary: what each node did over the whole window.

    The aggregate the live stream cannot give — a count, a median, a best — one record per
    node heard, most recently heard first.

    Drawn **for a person only**. Every figure in it is computable from the records that
    scrolled past above it, and emitting it as a second *shape* of line in the middle of an
    otherwise homogeneous NDJSON stream would cost every consumer a discriminator it would
    never otherwise need.

    Args:
        heard: Aggregated per-node statistics for the window.

    Returns:
        The listing.
    """
    from ..ui import fields
    from ..ui.report import Listing

    return Listing(
        key="heard",
        columns=(
            fields.node("node", lanes=(("hash", "NODE"), ("name", "NAME"))),
            fields.integer("packets", "PKTS"),
            fields.snr("median_snr_db", "MEDIAN_SNR_DB"),
            fields.snr("best_snr_db", "BEST_SNR_DB"),
            fields.decimal("last_rssi_dbm", "RSSI_DBM", ".0f"),
            fields.when("last_heard_at", "HEARD"),
            fields.position(),
        ),
        rows=[
            {
                "node": NodeRef(
                    name=node.name,
                    key=(node.public_key or "").lower() or None,
                    hash=node.node,
                    type=NODE_TYPE_LABELS.get(node.node_type),
                ),
                "packets": node.count,
                "median_snr_db": node.median_snr,
                "best_snr_db": node.best_snr,
                "last_rssi_dbm": node.last_rssi,
                "last_heard_at": node.last_seen,
                "position": _position(node.lat, node.lon),
            }
            for node in heard
        ],
        order=(
            "NODE",
            "NAME",
            "PKTS",
            "MEDIAN_SNR_DB",
            "BEST_SNR_DB",
            "RSSI_DBM",
            "HEARD",
            "LOCATION",
        ),
        plain_only=True,
    )
