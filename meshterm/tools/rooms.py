# SPDX-License-Identifier: Apache-2.0
"""The ``rooms`` tool: the room servers that your device knows, and how to join them.

In the menu, the tool opens the Rooms page (refer to :mod:`meshterm.ui.rooms`). The page
lists each room server that the device knows, with the joined rooms first. Each room has a
page where the user can join it, log in again, open its board, or forget it. On the CLI,
the tool gives ``list``, ``join``, and ``forget``.

The user reads the board of a room in Chat, as for the messages of a channel. This tool
owns all the steps to get in, as the ``channels`` tool owns all the configuration of a
slot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.models import NODE_TYPE_LABELS, Contact, LoginResult
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.fields import NodeRef
    from ..ui.report import Facts, Listing


@register
class RoomsTool(Tool):
    """Join room servers, and keep a list of the rooms that you joined."""

    name = "rooms"
    title = "Rooms"
    icon = "📌"
    help = "Join room servers' message boards"
    category = "Message"
    order = 25  # next to Channels: the other half of the items that Chat lists

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Open the Rooms page (menu) or run a scripted action (CLI).

        Args:
            ctx: The shared application context.
            params: A ``cli_action`` and its arguments (CLI), or empty for the menu.

        Returns:
            A :class:`ToolResult` that holds what occurred. The menu path shows nothing.
        """
        action = params.get("cli_action")
        if action == "join":
            return await self._cli_join(ctx, params)
        if action == "forget":
            return await self._cli_forget(ctx, params)
        if action == "list":
            return await self._cli_list(ctx)

        from ..ui.rooms import manage_rooms

        return ToolResult(summary={"logins": await manage_rooms(ctx)})

    # -- CLI --------------------------------------------------------------------

    async def _rooms(self, ctx: AppContext) -> list[Contact]:
        """Each room server that the device knows, as the contact cache of the session has it."""
        await ctx.device()
        return [c for c in await ctx.devstate.contacts() if c.is_room]

    async def _room(self, ctx: AppContext, needle: str) -> Contact:
        """The room server for a name or a key prefix. Any other value is a usage error."""
        folded = needle.casefold()
        contacts = await ctx.devstate.contacts()
        for contact in contacts:
            pub = (contact.public_key or "").lower()
            if contact.name.casefold() == folded or (folded and pub.startswith(folded)):
                if not contact.is_room:
                    raise typer.BadParameter(f"{contact.name!r} is not a room server")
                return contact
        raise typer.BadParameter(f"no room server matches {needle!r}")

    async def _cli_list(self, ctx: AppContext) -> ToolResult:
        """List each room server that the device knows: joined, access, heard, posts, unread."""
        rooms = await self._rooms(ctx)
        peers = [_peer(r) for r in rooms]
        lasts = ctx.repo.last_chat_messages(rooms=peers)
        records = []
        for room in sorted(rooms, key=lambda r: (not ctx.rooms.joined(r), r.name.casefold())):
            joined = ctx.rooms.joined(room)
            access = ctx.rooms.access(room) if joined else None
            last = lasts.get(f"dm:{_peer(room)}")
            records.append(
                {
                    "node": _node(room),
                    "joined": joined,
                    "access": access.value if access else None,
                    "heard_at": room.last_seen,
                    "last_post_at": last.created_at if last is not None else None,
                    "unread": ctx.chat.unread(f"dm:{_peer(room)}"),
                }
            )
        return ToolResult(
            summary={"rooms": len(records)},
            report=(_listing(records),),
            exit_code=exitcodes.OK if records else exitcodes.NO_RESULT,
        )

    async def _cli_join(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Log in to a room server (one exchange), and print the access that it gave.

        The password is ``--password``. If there is no ``--password``, it is the password
        that MeshTerm remembers for the room (its admin password, then its room password).
        MeshTerm remembers a password that works, exactly as the Rooms page remembers it.
        ``--flood`` first forgets the route to the room that the device learned. This is
        the solution for a stale route. Then the room sends each post that the device did
        not receive yet, one at a time. The posts wait on the device for the next program
        that reads it (``chat listen``, the menu). After that, ``chat history`` has them.

        A room never says no: a wrong password gets no reply. Thus no reply is a failure
        here (exit 4). The error message uses the words of the Rooms page: how the login
        went out, when the room was last heard, and if this password worked before.
        MeshTerm keeps the remembered password, if there is one.
        """
        from ..core.connection import DeviceCommandError
        from ..ui.rooms import silence_explanation, valid_password

        room = await self._room(ctx, str(params["room"]))
        password = params.get("password")
        if password is None:
            password = ctx.rooms.password(room)
        if password is None:
            raise typer.BadParameter(
                f"no password remembered for {room.name!r}; pass --password "
                "(an empty one asks the room whether it already knows you)"
            )
        verdict = valid_password(str(password))
        if verdict is not True:
            raise typer.BadParameter(str(verdict))
        login = await ctx.rooms.login(room, str(password), flood=bool(params.get("flood")))
        if login.result is LoginResult.REFUSED:
            raise DeviceCommandError(f"{room.name!r} refused the login; the password may be wrong")
        if login.access is None:
            explained = " ".join(silence_explanation(ctx, room, login, str(password)))
            hint = " Try --flood." if login.flood is False else ""
            raise DeviceCommandError(explained + hint)
        return ToolResult(
            summary={"room": room.name, "access": login.access.value},
            report=(_joined(room, login.access.value, login.flood),),
        )

    async def _cli_forget(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Do not log in to a room again, and forget its password. Its stored posts stay."""
        room = await self._room(ctx, str(params["room"]))
        was = ctx.rooms.joined(room)
        ctx.rooms.forget(room)
        return ToolResult(
            summary={"room": room.name, "forgotten": was},
            report=(_forgotten(room, was),),
            exit_code=exitcodes.OK if was else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``rooms`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        rooms_app = typer.Typer(help=self.help, no_args_is_help=True, rich_markup_mode=None)

        @rooms_app.command("list", help="Every room server your radio knows")
        def _list_cmd() -> None:
            run_tool_command(self, {"cli_action": "list"})

        @rooms_app.command("join", help="Log in to a room server, so it sends you its posts")
        def _join_cmd(
            room: str = typer.Argument(..., help="The room server's name or key prefix"),
            password: str | None = typer.Option(
                None, "--password", help="Room or admin password (else remembered)"
            ),
            flood: bool = typer.Option(
                False, "--flood", help="Forget the radio's route to the room first, and flood"
            ),
        ) -> None:
            run_tool_command(
                self,
                {"cli_action": "join", "room": room, "password": password, "flood": flood},
            )

        @rooms_app.command("forget", help="Stop logging in to a room, and forget its password")
        def _forget_cmd(
            room: str = typer.Argument(..., help="The room server's name or key prefix"),
        ) -> None:
            run_tool_command(self, {"cli_action": "forget", "room": room})

        app.add_typer(rooms_app, name=self.name)


# -- the reports ----------------------------------------------------------------------


def _peer(room: Contact) -> str:
    """The key prefix of the room, as its conversation stores it."""
    return (room.key_prefix or room.public_key[:12] or "").lower()


def _node(room: Contact) -> NodeRef:
    """A room as the shared node object."""
    from ..ui.fields import NodeRef

    return NodeRef(
        name=room.name,
        key=(room.public_key or "").lower() or None,
        hash=(room.key_prefix or "").lower() or None,
        type=NODE_TYPE_LABELS.get(room.node_type),
    )


def _listing(records: list[dict]) -> Listing:
    """``rooms list``: one room in each record, with the joined rooms first.

    ``ACCESS`` is ``-`` for a room that is not joined (``joined`` in the document says this
    directly). ``HEARD`` is the time when the room was last heard on the air. It is the
    only column that shows if a login can possibly get to the room.
    """
    from ..ui import fields
    from ..ui.report import Listing

    return Listing(
        key="rooms",
        columns=(
            fields.node("node", lanes=(("name", "ROOM"),)),
            fields.hidden("joined"),
            fields.word("access", "ACCESS"),
            fields.when("heard_at", "HEARD", absent="never"),
            fields.when("last_post_at", "LAST_POST", absent="never"),
            fields.integer("unread", "UNREAD"),
        ),
        rows=records,
        order=("ROOM", "ACCESS", "HEARD", "LAST_POST", "UNREAD"),
    )


def _joined(room: Contact, access: str, flood: bool | None) -> Facts:
    """What ``rooms join`` got: only the access that a room gave, the one fact asked for.

    The room, and how the login went out, are in the document. The plain face prints only
    the access word (``member``, ``admin``, or ``read-only``). A script uses this word to
    decide if it can post.
    """
    from ..ui import fields
    from ..ui.report import BARE, Facts

    return Facts(
        key="joined",
        fields=(
            fields.node("room", lanes=()),
            fields.word("access", "access"),
            fields.hidden("route"),
        ),
        values={
            "room": _node(room),
            "access": access,
            "route": None if flood is None else ("flood" if flood else "direct"),
        },
        shape=BARE,
        bare="access",
    )


def _forgotten(room: Contact, was: bool) -> Facts:
    """What ``rooms forget`` did: nothing on the plain face, the room and the fact in JSON."""
    from ..ui import fields
    from ..ui.report import SILENT, Facts

    return Facts(
        key="forgotten",
        fields=(fields.node("room", lanes=()), fields.flag("forgotten", "forgotten")),
        values={"room": _node(room), "forgotten": was},
        shape=SILENT,
    )
