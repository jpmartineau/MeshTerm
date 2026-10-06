# SPDX-License-Identifier: Apache-2.0
"""The ``courier`` tool: store-and-forward messages for contacts that MeshTerm cannot reach yet.

In the menu, the tool opens the outbox screen (refer to :mod:`meshterm.ui.courier_screen`).
The screen shows the queued messages with their live state, the flow to queue a message
(recipient → message → when), forced sends, and the history of the messages that were
delivered or that the courier gave up. Delivery runs in the background for the whole
session (refer to :mod:`meshterm.services.courier`). A queued message goes out when
MeshTerm next hears its contact, or at its scheduled time. It goes as a normal direct
message, with acknowledgement tracking and a polite exponential backoff. The result lights
the Watchtower badge in the header.

On the CLI, subcommands give script access to the same outbox:

- ``queue`` adds a message for the courier of the next interactive session to deliver. It
  reads the contact list to address the message, but it transmits nothing.
- ``send`` forces one delivery try now.
- ``list``, ``cancel``, and ``clear`` examine the outbox and remove entries from it.

These subcommands are the script form of the Send-now, Cancel, and Clear-finished actions
of the outbox screen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.courier_store import QueuedMessage
    from ..ui.report import Facts, Listing


@register
class CourierTool(Tool):
    """Queue messages for offline contacts. A message goes out when its contact is next heard."""

    name = "courier"
    title = "Courier"
    icon = "📨"
    help = "Outbox that delivers when a contact is heard"
    category = "Message"
    order = 30  # after Chat and Channels: the same conversations, but without the wait

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the outbox screen. It has no parameters to collect.

        The screen shows all its content itself, and it returns when the user leaves it.
        Thus the return value ``None`` tells the menu that the run is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.courier_screen import open_courier

        await open_courier(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run the handler for a scripted CLI action (the menu path is :meth:`prompt_params`).

        Args:
            ctx: The shared application context.
            params: A ``cli_action`` that names the outbox action, and its arguments.

        Returns:
            A :class:`ToolResult` that gives the result of the action.
        """
        action = params.get("cli_action", "queue")
        if action == "list":
            return await self._cli_list(ctx)
        if action == "send":
            return await self._cli_send(ctx, params)
        if action == "cancel":
            return await self._cli_cancel(ctx, params)
        if action == "clear":
            return await self._cli_clear(ctx)
        return await self._cli_queue(ctx, params)

    # -- CLI --------------------------------------------------------------------

    async def _cli_queue(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Queue one message from the CLI. This transmits nothing.

        Args:
            ctx: The shared application context.
            params: ``contact`` (the contact name), ``text`` (the message body), and an
                optional local clock time ``at`` (``HH:MM``, the next occurrence).

        Returns:
            A :class:`ToolResult` that describes the queued entry.
        """
        from ..ui.courier_screen import parse_clock
        from ..ui.watchtower_screen import contact_watch_key

        device = await ctx.device()
        contacts = await device.get_contacts()
        name = str(params["contact"])
        needle = name.casefold()
        contact = next((c for c in contacts if c.name.casefold() == needle), None)
        # The problem is the named contact, not the device. Nothing is queued and nothing
        # is sent, so these are bad arguments (2) instead of device failures (4).
        if contact is None:
            raise typer.BadParameter(f"unknown contact: {name!r}")
        key = contact_watch_key(contact)
        if key is None:
            raise typer.BadParameter(f"{name!r} has no usable key to address")

        not_before = None
        if params.get("at"):
            not_before = parse_clock(str(params["at"]))
            if not_before is None:
                raise typer.BadParameter(
                    f"--at wants a clock time like 07:00, not {params['at']!r}"
                )
        message = ctx.courier_store.queue(
            key, contact.name, str(params["text"]), not_before=not_before
        )
        when = (
            f"at {not_before.astimezone().strftime('%b %d %H:%M')}"
            if not_before is not None
            else "when the contact is next heard"
        )
        ctx.ui.ack(
            f"[ok]queued[/ok] #{message.ident} for [brand]{contact.name}[/brand] — "
            f"delivers {when} (an interactive session's courier does the sending)"
        )
        return ToolResult(
            summary={"queued": message.ident, "contact": contact.name, "at": params.get("at")},
            report=(_queued(message, contact.name, not_before),),
        )

    async def _cli_list(self, ctx: AppContext) -> ToolResult:
        """Give the outbox: first the waiting entries, then the finished entries.

        This is a read-only listing of the stored outbox. It does not use the device, so it
        works the same when a device is connected and when it is not. It is the script
        form of the outbox screen.
        """
        entries = ctx.courier_store.entries()
        return ToolResult(
            summary={"entries": len(entries)},
            report=(_outbox(entries),),
            exit_code=exitcodes.OK if entries else exitcodes.NO_RESULT,
        )

    async def _cli_send(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Try one forced delivery of a waiting entry, now.

        This is the script form of ``Send now``. It tries one forced delivery through the
        same courier that the outbox screen uses. It does not do the eligibility check
        ("wait until the contact is heard").
        """
        ident = int(params["id"])
        if ctx.courier_store.get(ident) is None:
            # A bad argument, not a device failure. The outbox is a local file, nothing was
            # transmitted, and no retry can make an id exist. Exit 4 told a caller to try
            # again. Exit 2 tells it to correct the command, and that is the true answer.
            raise typer.BadParameter(f"no outbox entry #{ident} (see `courier list`)")
        outcome = await ctx.courier.attempt_now(ident)
        notes = {
            "delivered": "[ok]✓ delivered[/ok] — acknowledged by the contact",
            "no ack": "[warn]sent, but no acknowledgement[/warn] — it stays queued for retry",
            "gave up": "[err]✗ gave up[/err] — the retry budget is spent",
            "unknown contact": (
                "[warn]the device's contacts don't know this contact yet[/warn] — it stays queued"
            ),
            "busy": "[muted]another delivery is in flight — try again in a moment[/muted]",
            "gone": "[muted]that entry is no longer waiting[/muted]",
        }
        ctx.ui.ack(notes.get(outcome, outcome))
        return ToolResult(
            summary={"id": ident, "outcome": outcome},
            report=(_attempted(ident, outcome),),
        )

    async def _cli_cancel(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Remove a waiting entry from the outbox (for a finished entry, use ``clear``)."""
        ident = int(params["id"])
        if ctx.courier_store.cancel(ident):
            ctx.ui.ack(f"[warn]cancelled outbox entry #{ident}[/warn]")
            return ToolResult(
                summary={"id": ident, "cancelled": True},
                report=(_did(("id", ident), ("cancelled", True)),),
            )
        ctx.ui.ack(f"[muted]no waiting entry #{ident} to cancel[/muted]")
        return ToolResult(
            summary={"id": ident, "cancelled": False},
            report=(_did(("id", ident), ("cancelled", False)),),
            exit_code=exitcodes.NO_RESULT,
        )

    async def _cli_clear(self, ctx: AppContext) -> ToolResult:
        """Remove each finished entry (delivered or given up), and keep the queue as it is."""
        before = len(ctx.courier_store.entries())
        ctx.courier_store.clear_done()
        cleared = before - len(ctx.courier_store.entries())
        if cleared:
            ctx.ui.ack(f"[ok]cleared {cleared} finished entr{'y' if cleared == 1 else 'ies'}[/ok]")
        else:
            ctx.ui.ack("[muted]no finished entries to clear[/muted]")
        return ToolResult(
            summary={"cleared": cleared},
            report=(_did(("cleared", cleared)),),
            exit_code=exitcodes.OK if cleared else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``courier`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        courier_app = typer.Typer(help=self.help, no_args_is_help=True, rich_markup_mode=None)

        @courier_app.command(
            "queue", help="Queue a message for delivery when the contact is next heard"
        )
        def _queue_cmd(
            contact: str = typer.Argument(..., help="The recipient contact's name"),
            text: list[str] = typer.Argument(..., help="The message to queue"),
            at: str | None = typer.Option(
                None, "--at", help="Hold until this local time (HH:MM, next occurrence)"
            ),
        ) -> None:
            tool_params: dict[str, Any] = {
                "cli_action": "queue",
                "contact": contact,
                "text": " ".join(text),
            }
            if at is not None:
                tool_params["at"] = at
            run_tool_command(self, tool_params)

        @courier_app.command("list", help="Show the outbox — waiting and finished entries")
        def _list_cmd() -> None:
            run_tool_command(self, {"cli_action": "list"})

        @courier_app.command("send", help="Force one delivery attempt for a waiting entry now")
        def _send_cmd(
            id: int = typer.Argument(..., help="Outbox entry id (from `courier list`)"),
        ) -> None:
            run_tool_command(self, {"cli_action": "send", "id": id})

        @courier_app.command("cancel", help="Remove a waiting entry from the outbox")
        def _cancel_cmd(
            id: int = typer.Argument(..., help="Outbox entry id (from `courier list`)"),
        ) -> None:
            run_tool_command(self, {"cli_action": "cancel", "id": id})

        @courier_app.command("clear", help="Drop finished (delivered / given-up) entries")
        def _clear_cmd() -> None:
            run_tool_command(self, {"cli_action": "clear"})

        app.add_typer(courier_app, name=self.name)


def _recipient(name: str | None, node_key: str | None) -> object:
    """The addressee of the outbox entry, as the shared node shape.

    The outbox stores a name and the key prefix (12 hex digits) that it addresses. Thus
    three of the five fields are correctly ``null``. The key prefix goes in ``hash``
    instead of ``key``, because it is a short id, not a full key. A key is the full value,
    and MeshTerm never truncates a key by its own choice. If MeshTerm calls a six-byte
    prefix a key, it gives a caller a value that ``--to`` cannot accept.
    """
    from ..ui.fields import NodeRef

    return NodeRef(name=name, hash=(node_key or "").lower() or None)


def _queued(message: QueuedMessage, name: str, not_before: object) -> Facts:
    """What ``courier queue`` put in the outbox.

    The plain face prints **only the id**, because the id is the only fact that a caller
    must keep: ``send`` and ``cancel`` take it. All the other data on the line is what the
    caller typed. The document holds the rest, because a caller that logs the queue has no
    other record of it.
    """
    from ..ui import fields
    from ..ui.report import Facts

    return Facts(
        key="queued",
        fields=(
            fields.integer("id", "id"),
            fields.node("node", lanes=()),
            fields.hidden("scheduled_at"),
            fields.hidden("text"),
        ),
        values={
            "id": message.ident,
            "node": _recipient(name, message.node_key),
            "scheduled_at": not_before,
            "text": message.text,
        },
    )


def _outbox(entries: list) -> Listing:
    """The outbox: first the waiting entries, then the finished entries.

    The two time columns are different on purpose. ``SCHEDULED`` is an appointment that
    the caller set with ``--at``, so it stays a wall-clock instant. ``FINISHED`` is an
    age, because a person who reads a delivered row wants to know how long ago it went.
    Thus the two columns have different widths, but both columns are correct.
    ``--absolute`` makes them the same for a person who does not like the difference.
    """
    from ..core.courier_store import DELIVERED, QUEUED
    from ..ui import fields
    from ..ui.report import Listing

    rows = []
    for m in entries:
        if m.status == QUEUED:
            state = "waiting"
        elif m.status == DELIVERED:
            state = "delivered"
        else:
            state = "gave up"
        rows.append(
            {
                "id": m.ident,
                "state": state,
                "node": _recipient(m.node_name, m.node_key),
                # An entry with no scheduled time goes when the contact is next heard. That
                # is not a time, so this field has no time either.
                "scheduled_at": m.not_before,
                "finished_at": m.finished,
                # Not shortened as the outbox screen shortens it, because nothing wraps here.
                "text": m.text,
            }
        )
    return Listing(
        key="outbox",
        columns=(
            fields.integer("id", "ID"),
            fields.word("state", "STATE"),
            fields.node("node", lanes=(("name", "NODE"),)),
            fields.instant("scheduled_at", "SCHEDULED"),
            fields.when("finished_at", "FINISHED"),
            fields.free("text", "TEXT"),
        ),
        rows=rows,
    )


def _attempted(ident: int, outcome: str) -> Facts:
    """The result of one forced delivery try.

    ``outcome`` is one value of the closed set that the help gives. It is kept exactly,
    with its spaces. Thus both faces show the same word, and a script that matches
    ``no ack`` matches the text that a person reads.
    """
    from ..ui import fields
    from ..ui.report import Facts

    return Facts(
        key="attempt",
        fields=(fields.hidden("id"), fields.word("outcome", "outcome")),
        values={"id": ident, "outcome": outcome},
    )


def _did(*facts: tuple[str, object]) -> Facts:
    """An acknowledgement as data: nothing on the plain face, and a document for a log.

    ``cancel`` and ``clear`` give all their information in their exit status, and that is
    the correct plain answer. But a caller that wants to store what was cancelled has no
    other place to read it. The document is for that caller.
    """
    from ..ui import fields
    from ..ui.report import SILENT, Facts

    def column(key: str, value: object):  # noqa: ANN202 - one of two shapes
        return fields.flag(key, key) if isinstance(value, bool) else fields.integer(key, key)

    return Facts(
        key="outcome",
        fields=tuple(column(key, value) for key, value in facts),
        values=dict(facts),
        shape=SILENT,
    )
