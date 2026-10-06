# SPDX-License-Identifier: Apache-2.0
"""The Trophy case tool: the record-setting walks that the trace tools found.

The menu face opens the browser (:mod:`meshterm.ui.records_screen`). It shows the records
of each discipline: longest distance, farthest node, most nodes (with and without
revisits), weakest link that survived, and biggest loop. Each discipline has its own
description. The user can open a record to see all its stats, walk it again in Trace
path, or delete it. The records are earned on the trace screens: each walk that comes
back is scored and offered to the boards (refer to :mod:`meshterm.services.records`). This
tool never transmits.

The CLI face prints the stored boards. ``meshterm records`` reads the tables, so that a
script (or a user at a shell) can see the standings without a connected device.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..services.records import CATEGORIES, CATEGORY_BY_ID, Category
from ..ui.script import NONE
from .base import Tool, ToolResult, register

#: The hash widths at which a record can be set: MeshCore path hashes are 1, 2, or 4 bytes.
WIDTHS = (1, 2, 4)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..persistence.repository import DiscoveredPath
    from ..ui.fields import NodeRef
    from ..ui.report import Column


@register
class TrophyCaseTool(Tool):
    """The record-setting mesh walks that the trace tools scored, to browse or to list."""

    name = "records"
    title = "Trophy case"
    icon = "🏆"
    help = "Record-setting walks, scored from every trace"
    category = "Explore"
    order = 40  # the two walks above it put their scores here

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the browser. The return value ``None`` completes the run with no run row.

        The screen reads stored records and transmits nothing (and it logs no rows of its
        own). Thus it is in :meth:`prompt_params`, and it does not need a logged run. The
        mesh walk and the dashboard use the same pattern.

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.records_screen import open_records

        await open_records(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print the stored boards. Only a scripted call comes here (this never transmits).

        Args:
            ctx: The shared application context.
            params: The ``category`` and ``width`` filters, and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the record count.
        """
        return self._show_records(
            ctx,
            category=params.get("category"),
            width=params.get("width"),
        )

    def _show_records(
        self, ctx: AppContext, *, category: str | None, width: int | None
    ) -> ToolResult:
        """Give the stored record boards (with no device, and with no transmission).

        Args:
            ctx: The shared application context.
            category: Only this category id, or ``None`` for all the disciplines.
            width: Only this hash width, or ``None`` for all the widths.

        Returns:
            A :class:`ToolResult` with the record count.
        """
        from ..services import trace_runner
        from ..ui import fields
        from ..ui.report import Listing

        wanted = [CATEGORY_BY_ID[category]] if category in CATEGORY_BY_ID else CATEGORIES
        # The names come only from the stored history, not from the contacts, because this
        # command reads the database. It must work with no device connected.
        resolve = trace_runner.make_node_resolver(None, ctx.repo.node_names())

        rows: list[dict] = []
        for cat in wanted:
            found = ctx.repo.discoveries(cat.id, width_bytes=width)
            found.sort(key=lambda r: (r.width_bytes, r.score if cat.ascending else -r.score))
            for record in found:
                floor = cat.id == "long_haul" and not record.stats.get("km_complete", True)
                rows.append(
                    {
                        "category": cat.id,
                        "hash_bytes": record.width_bytes,
                        "score": _Score(record.score, cat, floor),
                        "unit": cat.unit,
                        "lower_bound": floor,
                        "recorded_at": record.discovered_at,
                        "version": record.app_version,
                        "path": record.spec or None,
                        "route": _route(record, resolve),
                    }
                )
        listing = Listing(
            key="records",
            columns=(
                fields.word("category", "CATEGORY"),
                # The word of the contract for this concept. The plain heading stays
                # WIDTH, because a column of 1, 2, or 4 reads as a width.
                fields.integer("hash_bytes", "WIDTH"),
                _score_column(),
                fields.word("unit", "UNIT"),
                # `>=273.0` is not a number. A consumer that compares scores must see the
                # qualifier without a string match on a prefix of its own data.
                fields.hidden("lower_bound"),
                fields.when("recorded_at", "RECORDED"),
                fields.word("version", "VERSION"),
                # The transmitted spec, aligned hop for hop with the route next to it. It
                # has no column, because a route line already goes past the right edge.
                # But a caller uses it to walk the path again, so the document holds it.
                fields.hidden("path"),
                fields.route(),
            ),
            rows=rows,
            order=("CATEGORY", "WIDTH", "SCORE", "UNIT", "RECORDED", "VERSION", "ROUTE"),
        )
        return ToolResult(
            summary={"records": len(rows)},
            message=f"{len(rows)} record{'s' if len(rows) != 1 else ''} stored",
            report=(listing,),
            exit_code=exitcodes.OK if rows else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``records`` subcommand (a read-only listing of the records).

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(
            name=self.name,
            help="List the record-setting walks (reads the database; never transmits)",
        )
        def _records(
            category: str | None = typer.Option(
                None,
                "--category",
                "-c",
                help="Only this discipline (long_haul, far_point, long_leg, "
                "grand_tour, clean_trail, thin_thread, big_loop)",
            ),
            width: int | None = typer.Option(
                None, "--width", "-w", help="Only records at this hash width (1, 2, or 4)"
            ),
        ) -> None:
            tool_params: dict[str, Any] = {}
            if category:
                # Before, an unknown id selected all the disciplines. Thus a misspelled
                # `--category long_hual` returned "no records" with exit 5, which is the
                # same as a real empty result. A closed set refuses an unknown id.
                if category not in CATEGORY_BY_ID:
                    choices = ", ".join(CATEGORY_BY_ID)
                    raise typer.BadParameter(
                        f"--category must be one of: {choices} (got {category!r})"
                    )
                tool_params["category"] = category
            if width is not None:
                # A closed set, as for --category, for the same reason. `--width 3` found
                # nothing and exited 5, which was the same as an empty database. And
                # `--width 0` was falsy, so it listed all the widths with no warning.
                if width not in WIDTHS:
                    choices = ", ".join(map(str, WIDTHS))
                    raise typer.BadParameter(f"--width must be one of: {choices} (got {width})")
                tool_params["width"] = width
            run_tool_command(self, tool_params)


@dataclass(frozen=True, slots=True)
class _Score:
    """The score of a record, and the one fact about it that a bare number cannot hold.

    ``format_score`` writes ``273.0 km`` for a person. Here the unit has its own column,
    so that the ``SCORE`` column has one comparable number in each row. But one thing must
    stay with the number: the ``>=`` on a Longest-distance walk whose kilometre total is
    not complete. A hop on that walk has no known position, so the distance is a minimum,
    not a measurement. Without the qualifier, a lower bound becomes a claim.

    The plain face shows the prefix. The document keeps the number as a number, and it
    gives ``lower_bound`` next to it. The reason is that a consumer that compares scores
    must not have to match a prefix at the start of its own data.
    """

    value: float
    category: Category
    lower_bound: bool

    @property
    def text(self) -> str:
        """The score as the SCORE column prints it, with the qualifier."""
        if self.category.unit == "nodes":
            drawn = str(int(self.value))
        elif self.category.unit == "dB":
            drawn = f"{self.value:+.1f}"
        else:
            drawn = f"{self.value:.1f}"
        return f">={drawn}" if self.lower_bound else drawn


def _score_column() -> Column:
    """The SCORE column: the qualified number on the plain face, the bare number in a document."""
    from ..ui.report import Column, Lane

    return Column(
        key="score",
        lanes=(
            Lane(
                header="SCORE",
                render=lambda s: s.text if s is not None else NONE,
                align="right",
            ),
        ),
        json=lambda s: None if s is None else s.value,
    )


def _route(record: DiscoveredPath, resolve: Callable[[str], str | None]) -> list[NodeRef]:
    """The walk of a record, as the shared route shape.

    Each hop has a name if the history knows the node. Each hop also has the hash with
    which it was transmitted (the hop of the spec itself, at the width of this record).
    Thus a person can compare the line with the ``path`` that a new walk uses. If the
    two lists have different lengths, the node id is used instead. That id is the only
    identity that the hop has.

    Our node is never in the walk of a record. A discovery is scored on what it reached,
    and its two ends are the nodes of other persons.

    Args:
        record: The record whose walk to render.
        resolve: Gives the friendly name of a node id, or ``None`` if the name is unknown.

    Returns:
        The hops, in the order that the packet went through them.
    """
    from ..ui.fields import NodeRef

    spec = record.spec.split(",") if record.spec else []
    hops: list[NodeRef] = []
    for index, node in enumerate(record.route):
        addressed = spec[index] if index < len(spec) else node
        hops.append(NodeRef(name=resolve(node), hash=addressed))
    return hops
