# SPDX-License-Identifier: Apache-2.0
"""The Trophy case tool: the record-setting walks the trace tools have turned up.

The menu face opens the browser (:mod:`meshterm.ui.records_screen`): every discipline's
records — longest distance, farthest node, most nodes (with and without revisits),
weakest surviving link, biggest loop — each under its own description, opened for their
full stats, re-walked in Trace path, or deleted. Records are *earned* on the trace
screens, where every walk that comes home is scored and offered to the boards (see
:mod:`meshterm.services.records`); this tool never transmits.

The CLI face prints the stored boards: ``meshterm records`` reads the tables so a script
(or a curious shell) can see the standings without a device attached.
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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..persistence.repository import DiscoveredPath
    from ..ui.fields import NodeRef
    from ..ui.report import Column


@register
class TrophyCaseTool(Tool):
    """The record-setting mesh walks the trace tools scored, browsed or listed."""

    name = "records"
    title = "Trophy case"
    icon = "🏆"
    help = "Record-setting walks, scored from every trace"
    category = "Explore"
    order = 40  # what the two walks above it score into

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the browser; returning ``None`` completes the invocation with no run row.

        The screen reads stored records and transmits nothing (and logs no rows of its
        own), so it lives in :meth:`prompt_params` and needs no logged run — the mesh walk /
        dashboard pattern.

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.records_screen import open_records

        await open_records(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print the stored boards — only reachable from a scripted call (never transmits).

        Args:
            ctx: Shared application context.
            params: ``category``/``width`` filters and the injected ``_run_id``.

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
        """State the stored record boards (no device, no transmissions).

        Args:
            ctx: Shared application context.
            category: Only this category id, or ``None`` for every discipline.
            width: Only this hash width, or ``None`` for every width.

        Returns:
            A :class:`ToolResult` with the record count.
        """
        from ..services import trace_runner
        from ..ui import fields
        from ..ui.report import Listing

        wanted = [CATEGORY_BY_ID[category]] if category in CATEGORY_BY_ID else CATEGORIES
        # Names come from stored history alone — no contacts, because this command reads
        # the database and must work with no radio attached at all.
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
                # The contract's word for the concept. The plain heading stays WIDTH,
                # which is what a column of 1/2/4 reads as.
                fields.integer("hash_bytes", "WIDTH"),
                _score_column(),
                fields.word("unit", "UNIT"),
                # `>=273.0` is not a number, and a consumer comparing scores must be able
                # to see the qualifier without string-matching a prefix off its own data.
                fields.hidden("lower_bound"),
                fields.when("recorded_at", "RECORDED"),
                fields.word("version", "VERSION"),
                # The transmitted spec, hop-aligned with the route beside it. It has no
                # column — a route line already runs off the right — but it is what a
                # caller re-walks with, so the document carries it.
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
        """Register the ``records`` subcommand (read-only record listing).

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
                # An unknown id used to select *every* discipline, so a misspelled
                # `--category long_hual` came back as "no records" — exit 5, which is
                # exactly what a real empty result looks like. A closed set refuses.
                if category not in CATEGORY_BY_ID:
                    choices = ", ".join(CATEGORY_BY_ID)
                    raise typer.BadParameter(
                        f"--category must be one of: {choices} (got {category!r})"
                    )
                tool_params["category"] = category
            if width is not None:
                if width not in (1, 2, 4):
                    raise typer.BadParameter(f"--width must be one of: 1, 2, 4 (got {width!r})")
                tool_params["width"] = width
            run_tool_command(self, tool_params)


@dataclass(frozen=True, slots=True)
class _Score:
    """A record's score, and the one thing about it a bare number cannot carry.

    ``format_score`` writes ``273.0 km`` for a reader; the unit is its own column here so
    the ``SCORE`` column is one comparable number per row. What has to travel *with* the
    number is the ``>=`` on a Longest-distance walk whose kilometre total is incomplete —
    some hop on it has no known position, so the distance is a floor rather than a
    measurement, and dropping the qualifier would turn a lower bound into a claim.

    The plain face wears the prefix; the document keeps the number a number and says
    ``lower_bound`` beside it, because a consumer comparing scores cannot be asked to
    string-match a prefix off the front of its own data.
    """

    value: float
    category: Category
    lower_bound: bool

    @property
    def text(self) -> str:
        """The score as the SCORE column prints it, qualifier and all."""
        if self.category.unit == "nodes":
            drawn = str(int(self.value))
        elif self.category.unit == "dB":
            drawn = f"{self.value:+.1f}"
        else:
            drawn = f"{self.value:.1f}"
        return f">={drawn}" if self.lower_bound else drawn


def _score_column() -> Column:
    """The SCORE column: the qualified figure plain, the bare number in the document."""
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
    """A record's walk as the shared route shape.

    Each hop is named where history knows the node and carries the hash it was actually
    transmitted at — the spec's own hop, at this record's width — so the line can be read
    against the ``path`` a re-walk would take. Where the two lists disagree in length the
    node id stands in, which is the only identity that hop has.

    Our own node never appears in a record's walk: a discovery is scored on what it
    reached, and the two ends of it are somebody else's.

    Args:
        record: The record whose walk to render.
        resolve: Maps a node id to a friendly name, or ``None`` when unknown.

    Returns:
        The hops in propagation order.
    """
    from ..ui.fields import NodeRef

    spec = record.spec.split(",") if record.spec else []
    hops: list[NodeRef] = []
    for index, node in enumerate(record.route):
        addressed = spec[index] if index < len(spec) else node
        hops.append(NodeRef(name=resolve(node), hash=addressed))
    return hops
