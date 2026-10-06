# SPDX-License-Identifier: Apache-2.0
"""The ``monitor`` tool: a foreground capture of the mesh traffic, for a limited time.

Passive monitoring stores each advert and telemetry packet that the companion hears in the
database, with its SNR, its RSSI, and any shared location. Over time, this makes the
history that the packet counts of the map and the nodes list read. The tool transmits
nothing. It only listens.

MeshTerm always stores this history. The session-wide
:class:`~meshterm.services.event_hub.EventHub` receives each packet, and
:class:`~meshterm.services.monitor_service.MonitorService` (``ctx.monitor``) is one of its
subscribers. From the time that the connection to the device opens, the service writes
the packets to the history. In the menu, the live packet counters show in the header,
which is always visible, and the stored data shows on Nodes and Map. Thus this tool has no
screen, and it is CLI only. ``meshterm monitor --seconds 60`` prints each heard packet to
the console, and it gives a summary of the window when the window ends.
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
    """Capture heard packets in the foreground for a limited time window (CLI only)."""

    name = "monitor"
    title = "Monitor"
    icon = "🎧"
    help = "Capture overheard packets live for a while and summarize them"
    category = "Watch"
    order = 50
    menu_visible = False  # always stored. In the menu, the header, Nodes, and Map show it

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run directly, without a logged ``runs`` row.

        The monitor service stores the observations of the capture under its own
        ``monitor`` run. The base :meth:`Tool.execute` puts each run in a run row. A second
        run row for this run logs the session two times.

        Args:
            ctx: The shared application context.
            params: The parameters for this run.

        Returns:
            The :class:`ToolResult` from :meth:`run`.
        """
        return await self.run(ctx, params)

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Store heard packets in the foreground for a limited time window.

        The method connects the device, and it makes sure that the history storage and
        the event hub run. Then it prints each heard packet to the console until the
        window ends (or until the user interrupts it). The monitor service stores the
        observations exactly as in an interactive session. This method adds a live
        console output, and a summary of the nodes that were heard in the window.

        Args:
            ctx: The shared application context.
            params: ``seconds``: the duration of the capture (``0`` or ``None`` = until
                Ctrl-C).

        Returns:
            A :class:`ToolResult` with the packet count and the node count of the window.
        """
        from ..ui import script

        seconds = int(params.get("seconds") or 0)
        await ctx.device()  # show a connection problem before the capture is announced
        await ctx.monitor.start()  # register history storage before the hub sends events
        await ctx.events.start()
        # The announcement is about the run, and it is not part of the answer. Thus it goes
        # to stderr, and it stays out of `meshterm monitor -s 60 > packets.txt`.
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
                    # The region that a flood was sent into, found from the region names
                    # that are known now. A direct packet (or a class with no route) has
                    # no region.
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
                    await asyncio.Event().wait()  # until Ctrl-C or cancellation
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
        # The window is over, so its facts are useful. They go to stderr, where all the
        # information about a run goes. Thus the packets, which are the reason that the
        # caller redirected the output, stay alone on stdout.
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
        """Register the ``monitor`` subcommand (the foreground capture for a limited time).

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
    """The shared position of a reception, or ``None`` if the packet had no position."""
    return Position(lat, lon) if lat is not None and lon is not None else None


def _relays(path: str | None) -> list[str] | None:
    """The relay chain of a packet as hashes, in the order that the packet went through.

    The function returns hashes instead of the shared node shape, and that is the correct
    answer. At reception time, nothing finds the node for a relay. Thus a node object for
    each hop has only four ``null`` values around a hash. ``[]`` is a direct reception
    (zero hops). ``None`` means that the packet class has no path, which is a different
    fact.
    """
    if path is None:
        return None
    return [hop for hop in path.split(",") if hop]


def _live_packets() -> Listing:
    """The shape that the live capture streams: one record for each packet, when it arrives.

    ``TIME`` is an absolute time, but the times of a listing are usually ages. The reason
    is the base of that rule: in a live output, an age shows ``now`` in each row. ``SCOPE``
    gives the region that a flood was sent into (:func:`~meshterm.ui.fields.scope`). In
    the document, it is ``null`` for a packet that has no scope. The **name goes last**,
    so the only field with no width cannot push a lane. This change finally let the two
    streams have columns, and with the columns, the quoting was no longer necessary.
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
            # The packet class. The plain stream never had space for it. This field is new,
            # and the simulator does not make each class. Thus a consumer must read an
            # unfamiliar word as a word, not as an error.
            fields.hidden("kind"),
            pin(fields.snr("snr_db", "SNR_DB"), 6),
            pin(fields.decimal("rssi_dbm", "RSSI_DBM", ".0f"), 8),
            pin(fields.position(), 19),
            # The scope of the flood: a region name, ``unknown``, or ``unscoped``, and ``-``
            # for a packet with no scope. The width is pinned to hold the two words
            # complete. A long region name overruns and pushes the row to the right, so
            # NAME still goes last.
            pin(fields.scope(), 10),
            fields.hidden("path"),
        ),
        order=("TIME", "NODE", "SNR_DB", "RSSI_DBM", "LOCATION", "SCOPE", "NAME"),
    )


def _heard(heard: list[HeardNode]) -> Listing:
    """A summary of the capture window for each node: what the node did in the whole window.

    The summary gives the aggregates that the live stream cannot give (a count, a median,
    a best value). It has one record for each heard node, and the most recently heard
    node is first.

    It is drawn **for a person only**. A program can calculate each value in it from the
    records above it. The NDJSON stream has lines of only one shape. If the summary was a
    second shape of line in that stream, each consumer must have a discriminator, which
    no consumer needs for the other lines.

    Args:
        heard: The aggregated statistics of each node for the window.

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
