# SPDX-License-Identifier: Apache-2.0
"""The ``clocks`` tool: how far the clock of each node is from ours.

A node with a wrong clock puts wrong times on its adverts and its messages. MeshTerm shows
the arrival time in place of a wrong sender time (refer to
:meth:`~meshterm.core.models.ChatMessage.corrected`), but the owner of the node is the only
one who can set the clock. This tool says which nodes to tell, and by how much they are off.

The menu opens the Clock drift page (refer to :mod:`meshterm.ui.clocks_screen`): a
histogram of the offsets, the managed nodes, and the nodes out of sync. Enter opens the
node page. The CLI prints one row for each sender with a clock sample in the window, the
largest offset first.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.models import NODE_TYPE_LABELS, Contact, utcnow
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.clocks_screen import ClockRow
    from ..ui.report import Listing


@register
class ClocksTool(Tool):
    """Show the clock offset of each node heard, and the nodes whose clocks are wrong."""

    name = "clocks"
    title = "Clock drift"
    icon = "🕒"
    help = "Nodes whose clocks are wrong — offsets, histogram"
    category = "Watch"
    order = 35  # after the Watchtower, before the Time machine: one more report on the mesh

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Measure the clock offsets of the window, and show them.

        Args:
            ctx: The shared application context.
            params: Not used.

        Returns:
            A :class:`ToolResult` with the counts, and the listing for the CLI.
        """
        from ..ui.surface import TuiUi

        tolerance = ctx.preferences.clock_tolerance_s
        rows = await clock_rows(ctx)
        off = sum(1 for r in rows if r.current and r.out_of_sync(tolerance))
        summary = {"senders": sum(1 for r in rows if r.current), "out_of_sync": off}
        if isinstance(ctx.ui, TuiUi):
            await _open_page(ctx, rows, tolerance)
            return ToolResult(summary=summary)
        # The hash has the path hash width of the device, as in ``contacts``: it is the
        # token that ``--path`` takes. A key prefix is not a hash.
        try:
            mode = await ctx.devstate.path_hash_mode()
        except Exception:  # noqa: BLE001 - an optional read. If it is absent, no hash.
            mode = None
        prefix_bytes = (mode + 1) if isinstance(mode, int) and 0 <= mode <= 3 else 0
        return ToolResult(
            summary=summary,
            report=(_listing(rows, tolerance, prefix_bytes),),
            exit_code=exitcodes.OK if rows else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``clocks`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _clocks() -> None:
            run_tool_command(self, {})


async def clock_rows(ctx: AppContext) -> list[ClockRow]:
    """One row for each sender with a clock sample in the window, and for each managed node.

    A keyed row gets the contact of the device when there is one, and else a contact made
    from the stored history, so that Enter can open its node page. Our own node is not a
    row: MeshTerm sets the clock of the companion itself when it connects.

    Each managed node is a row, also with no sample in the window (JP, 2026-11-03). Its
    offset is then the newest from all the history, which is not *current*, or ``None``
    when the node never sent a sample.

    Args:
        ctx: The shared application context.

    Returns:
        The rows, in no specific order.
    """
    from ..services.clock_offsets import node_offset, offsets, window_samples
    from ..services.trace_runner import make_name_key_resolver
    from ..ui.clocks_screen import ClockRow

    since = utcnow() - timedelta(days=ctx.preferences.clock_report_days)
    try:
        contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - the stored history still names the nodes
        contacts = []
    try:
        info = await ctx.devstate.self_info()
    except Exception:  # noqa: BLE001 - without our key, our own adverts are a row
        info = {}
    self_id = str(info.get("public_key") or "").lower().removeprefix("0x")[:12]
    stored_names, named_at = ctx.repo.node_names_with_times()
    key_of = make_name_key_resolver(contacts, stored_names, heard=named_at)
    found = offsets(window_samples(ctx.repo, since=since, key_of=key_of))
    by_id = {
        (c.public_key or c.key_prefix or "").lower().removeprefix("0x")[:12]: c for c in contacts
    }
    heard = {n.node: n for n in ctx.repo.heard_nodes() if n.node}
    admin = getattr(ctx, "admin_store", None)

    rows: list[ClockRow] = []
    for offset in found:
        if not offset.keyed:
            rows.append(ClockRow(offset, None, None, None, False, None))
            continue
        if self_id and offset.who == self_id:
            continue
        contact = by_id.get(offset.who)
        hn = heard.get(offset.who)
        name = (contact.name if contact else None) or stored_names.get(offset.who)
        name = name or (hn.name if hn else None)
        if contact is None:
            contact = Contact(
                name=name or offset.who,
                public_key=(hn.public_key or "") if hn else "",
                key_prefix=offset.who,
                last_seen=hn.last_seen if hn else None,
                node_type=hn.node_type if hn else None,
                lat=hn.lat if hn else None,
                lon=hn.lon if hn else None,
            )
        key = contact.public_key or contact.key_prefix
        managed = admin is not None and admin.get(contact) is not None
        rows.append(ClockRow(offset, name, key, contact.node_type, managed, contact))

    # The managed nodes, and the contacts whose adverts the device refuses, that sent no
    # sample in the window: the newest offset from all the history, or none.
    listed = {r.offset.who for r in rows if r.offset is not None and r.offset.keyed}
    for contact in contacts:
        node_id = (contact.public_key or contact.key_prefix or "").lower().removeprefix("0x")[:12]
        if not node_id or node_id in listed or node_id == self_id:
            continue
        managed = admin is not None and admin.get(contact) is not None
        if not managed and contact.adverts_refused_until is None:
            continue
        older = node_offset(ctx.repo, node_id, name=contact.name, key_of=key_of)
        key = contact.public_key or contact.key_prefix
        rows.append(
            ClockRow(older, contact.name, key, contact.node_type, managed, contact, current=False)
        )
    return rows


async def _open_page(ctx: AppContext, rows: list[ClockRow], tolerance: float) -> None:
    """Run the Clock drift page. Enter opens the node page, and Esc comes back to the row."""
    from ..ui.clocks_screen import SYNC_ALL, clock_drift_screen, clocks_to_sync
    from ..ui.node_detail_screen import open_node_detail
    from ..ui.repeater_admin import sync_clocks
    from ..ui.surface import TuiUi
    from ..ui.tui.screen import CANCEL

    assert isinstance(ctx.ui, TuiUi)
    session = ctx.ui.session
    screen = clock_drift_screen(rows, tolerance_s=tolerance)
    async with session.stay(screen) as visit:
        while True:
            chosen = await visit.result()
            if chosen is CANCEL or chosen is None:
                return
            if chosen == SYNC_ALL:
                # The page stays as it is: an offset changes only when a new advert of the
                # node arrives, so there is nothing to draw again now.
                await sync_clocks(ctx, clocks_to_sync(rows, tolerance))
                continue
            # The page opens to look. The verbs that manage a contact belong to the
            # Contacts list, and this page is not that list.
            await open_node_detail(ctx, chosen, manage=False)


def _listing(rows: list[ClockRow], tolerance: float, prefix_bytes: int = 0) -> Listing:
    """The senders, the largest offset first, stated one time for the two faces.

    ``OFFSET`` is the offset in words (``3h 12m behind``, or ``in sync`` within the
    tolerance). The document gives ``offset_s``, signed, a positive value for a clock
    ahead of ours. A sender with no key has only its name, and ``keyed`` is false. A managed
    node that never sent a sample is a row too, last: ``no sample``, and ``null`` in the
    document.

    Args:
        rows: The senders.
        tolerance: The ``clock_tolerance_s`` preference.
        prefix_bytes: The path hash width of the device. ``HASH`` is that many bytes of
            the key, so that ``--path`` takes it. ``0`` gives no hash.
    """
    from ..ui import fields
    from ..ui.report import Column, Lane, Listing
    from ..ui.widgets import format_offset

    def words(seconds: float | None) -> str:
        if seconds is None:
            return "no sample"
        return "in sync" if abs(seconds) <= tolerance else format_offset(seconds)

    def order(row: ClockRow) -> tuple:
        return (row.offset is None, -abs(row.offset.offset_s) if row.offset else 0, row.label)

    table = []
    for row in sorted(rows, key=order):
        offset = row.offset
        keyed = offset is None or offset.keyed
        key = (row.contact.public_key or "").lower() if row.contact else ""
        known = key or (offset.who if offset is not None and keyed else "")
        known = known or ((row.contact.key_prefix or "").lower() if row.contact else "")
        table.append(
            {
                "node": fields.NodeRef(
                    name=row.name or (None if keyed else row.label),
                    key=key or None,
                    hash=known[: prefix_bytes * 2] or None,
                    type=NODE_TYPE_LABELS.get(row.node_type),
                ),
                "offset_s": round(offset.offset_s) if offset is not None else None,
                "samples": offset.samples if offset is not None else 0,
                "last_sample_at": offset.last_at if offset is not None else None,
                "out_of_sync": row.out_of_sync(tolerance),
                "managed": row.managed,
                "keyed": keyed,
            }
        )
    return Listing(
        key="clocks",
        columns=(
            fields.node("node", lanes=(("name", "NAME"), ("type", "TYPE"), ("hash", "HASH"))),
            Column(
                key="offset_s",
                lanes=(Lane(header="OFFSET", render=words, align="right"),),
                json=lambda v: v,
            ),
            fields.integer("samples", "SAMPLES"),
            fields.when("last_sample_at", "LAST", absent="never"),
            fields.hidden("out_of_sync"),
            fields.flag("managed", "MANAGED"),
            fields.hidden("keyed"),
        ),
        rows=table,
    )
