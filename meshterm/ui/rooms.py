# SPDX-License-Identifier: Apache-2.0
"""The Rooms page: the room servers your radio knows, and joining them.

The Channels page's counterpart for rooms (see :mod:`meshterm.ui.channels`). A channel is
something the radio holds in a slot; a room is somewhere you log in to — so where Channels
creates and keys slots, this page lists every room server the radio knows, joined ones
first, and joins them: a password, one login, and what the room let us do. Once joined, a
room is in Chat, the way a channel is once it has a slot, and its board is read and posted
to there (:mod:`meshterm.ui.room`).

Joining is also where a room is hardest to understand, because a room never says *no*. A
wrong password gets no reply at all; so does a room out of reach, and so does a login sent
along a route the radio learned that has since gone stale. "No reply" alone is no help, so
the outcome is explained from what is actually known — how the login went out, when the
room was last heard, whether this password has worked before — and the way on is offered as
buttons: try by flood (which forgets the stale route first), try again, or try another
password. Each of those is one more login the reader asked for; none is ever sent on its own.

That flow — :func:`join_room` — is shared: the board's ^L runs it too, and the command
line's ``rooms join`` explains a silent room in the same words (:func:`silence_explanation`).

The page follows the Channels page's shape: one list kept pushed for the whole visit, each
room a detail page with its vital signs over its actions, every prompt floating over it.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text

from ..core.connection import ContactNotOnDeviceError
from ..core.models import (
    NODE_TYPE_ROOM,
    Contact,
    Conversation,
    LoginResult,
    RoomAccess,
    RoomLogin,
    is_room,
)
from ..platforms import get_platform
from .menus import (
    Lane,
    column_header,
    fit_cells,
    icon_lane,
    marked_label,
    menu_rows,
    section_heading,
)
from .theme import name_style
from .tui import CANCEL, Choice, SelectScreen, Separator
from .widgets import NODE_GLYPHS, age_seconds, format_age, format_ago

if TYPE_CHECKING:
    from ..context import AppContext

#: The page's footer sentence: navigation, then the filter, then the action, Esc last.
_PAGE_HINT = "↑↓ move · type to filter · Enter open · Esc back"

#: MeshCore copies at most this many bytes of a login password into the request
#: (``BaseChatMesh::sendLogin``) and drops the rest without a word, so a longer password
#: could never work — it is refused at the prompt instead of meeting silence on the air.
PASSWORD_BYTES = 15

# Detail-page action sentinels.
_OPEN = "open"
_JOIN = "join"
_RELOGIN = "relogin"
_PASSWORD = "password"
_FORGET = "forget"
_DELETE = "delete"

# What an outcome dialog's buttons answer with.
_RETRY_FLOOD = "flood"
_RETRY_PASSWORD = "password"
_RETRY = "again"
_RESTORE = "restore"

#: A room row's marker: the room server's ``■``, in its type colour (the map's, the
#: contact list's, the chat picker's).
_ROOM_MARK = NODE_GLYPHS[NODE_TYPE_ROOM]


# --- joining ---------------------------------------------------------------------------


def valid_password(text: str) -> bool | str:
    """A password a login can carry whole: :data:`PASSWORD_BYTES` bytes at most."""
    if len(text.encode("utf-8")) > PASSWORD_BYTES:
        return f"MeshCore sends at most {PASSWORD_BYTES} characters of a password"
    return True


async def ask_password(ctx: AppContext, room: Contact, default: str = "") -> str | None:
    """Ask for ``room``'s password; ``None`` when the reader backs out.

    A blank answer is a real one: it asks the room whether it already knows us, which it
    does for its admins and for anyone who has logged in since it last restarted.
    ``default`` fills the field (masked) with the password just tried, so trying it again
    is Enter and trying another is typing over it.
    """
    verb = "Log in" if ctx.rooms.joined(room) else "Join"
    return await ctx.ui.text(
        f"{verb} — {room.name}",
        prompt="Room password (blank if the room already knows you):",
        help_text="Ask whoever runs the room. One left on its stock settings uses hello.",
        default=default,
        validate=valid_password,
        password=True,
        floating=True,
    )


def silence_explanation(
    ctx: AppContext, room: Contact, login: RoomLogin, password: str
) -> list[str]:
    """Why a login met silence, in what is actually known — one sentence each.

    THE explanation, shared by the join dialogs and the command line. A room that doesn't
    answer could have missed the login, or heard it with the wrong password, and it says
    nothing either way; so instead of a guess, the facts that tell the two apart: how the
    radio sent it (a stale learned route is the commonest silent failure, and a flood is
    the way round it), when the room was last heard, and whether this password has ever
    worked here.

    Args:
        ctx: Shared application context.
        room: The room, as the session's contact cache holds it (its ``last_seen`` is the
            later of its own advert and our last reception of it).
        login: The login that met silence.
        password: The password it carried.

    Returns:
        The sentences, in reading order.
    """
    if login.flood is None:
        why = f": {login.radio_error}" if login.radio_error else ""
        return [f"Your radio didn't send the login{why}."]
    lines = [f"{room.name} didn't answer."]
    if login.flood:
        lines.append("The login went out to the whole mesh, as a flood.")
    else:
        hops = len(room.route_hops or ())
        span = f" ({hops} hop{'' if hops == 1 else 's'})" if hops else ""
        lines.append(
            f"It went along the route your radio learned to the room{span}, and a route "
            "goes stale when the mesh changes."
        )
    if room.last_seen is None:
        lines.append("The room has never been heard here.")
    else:
        ago = format_ago(age_seconds(room.last_seen))
        lines.append(
            "The room was heard just now." if ago == "now" else f"It was last heard {ago}."
        )
    if ctx.rooms.worked_before(room, password):
        lines.append("This password got you in before, so the room most likely didn't hear you.")
    else:
        lines.append(
            "A room doesn't answer a wrong password — it stays silent — so either it didn't "
            "hear you, or the password is wrong."
        )
    return lines


async def join_room(ctx: AppContext, room: Contact, *, ask: bool = False) -> RoomLogin | None:
    """Join ``room``, or log in to it again — THE explicit flow, explained to the end.

    The password remembered for the room is used unless ``ask`` (or there is none), in
    which case the reader is asked. The login runs under a modal card counting the seconds,
    since a room several hops out can take fifteen to answer. A login that lets the reader
    post ends the flow quietly — the page or board it was started from shows the access.
    Anything else is explained (:func:`_outcome`), and the reader picks what to try next;
    every retry is one more login they chose to send.

    Args:
        ctx: Shared application context (running the interactive TUI surface).
        room: The room server, as the session's contact cache holds it.
        ask: Ask for the password even when one is remembered.

    Returns:
        The last login's outcome, or ``None`` when the reader backed out before sending one.
    """
    password = None if ask else ctx.rooms.password(room)
    tried = ""
    flood = False
    last: RoomLogin | None = None
    while True:
        if password is None:
            password = await ask_password(ctx, room, default=tried)
            if password is None:
                return last
        try:
            last = await _log_in(ctx, room, password, flood=flood)
        except ContactNotOnDeviceError:
            if await _restore(ctx, room):
                continue
            return last
        except Exception as exc:  # noqa: BLE001 - every failure is said, none is fatal
            await ctx.ui.dialog(
                Text(f"The login to {room.name} failed: {exc}", style="err"),
                [("OK", None)],
                title=_title(ctx, room),
            )
            return last
        if last.access is not None and last.access.can_post:
            return last
        choice = await _outcome(ctx, room, last, password)
        if choice == _RETRY_FLOOD:
            flood = True
        elif choice == _RETRY:
            flood = False
        elif choice == _RETRY_PASSWORD:
            tried, password, flood = password, None, False
        elif choice == _RESTORE:
            if not await _restore(ctx, room, asked=True):
                return last
        else:
            return last


def _title(ctx: AppContext, room: Contact) -> str:
    """The flow's dialog title: ``Join — Room``, or ``Log in — Room`` once joined."""
    return f"{'Log in' if ctx.rooms.joined(room) else 'Join'} — {room.name}"


async def _log_in(ctx: AppContext, room: Contact, password: str, *, flood: bool) -> RoomLogin:
    """Send one login under a modal card that says how it went out and counts the wait."""
    if flood or room.route_hops is None:
        how = "by flood"
    elif room.route_hops:
        how = f"along its {len(room.route_hops)}-hop route"
    else:
        how = "directly"
    caption = f"logging in to {room.name} {how}…"
    started = time.monotonic()
    async with ctx.ui.busy_dialog(caption, title="Rooms") as busy:

        async def count() -> None:
            while True:
                await asyncio.sleep(1.0)
                busy.message = f"{caption} {int(time.monotonic() - started)} s"

        ticker = asyncio.ensure_future(count())
        try:
            return await ctx.rooms.login(room, password, flood=flood)
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass


async def _outcome(ctx: AppContext, room: Contact, login: RoomLogin, password: str) -> object:
    """Explain a login that didn't let us post, and ask what to try next.

    Two buttons at most — the safe way out and the one next step the evidence points at —
    so the row fits the handhelds' 45-cell dialog whole. A stale route is the first thing
    to rule out (its silence is the commonest, and the cheapest to test: the same password
    by flood); only once a flood has met silence is the password in question, and *Try
    again…* then reopens the prompt with it filled in, so Enter resends it and typing
    replaces it.

    Returns:
        The chosen button's value: one of the ``_RETRY*`` / ``_RESTORE`` sentinels, or
        ``None`` to stop.
    """
    title = _title(ctx, room)
    if login.access is RoomAccess.READ_ONLY:
        text = Text(f"You're in {room.name}, but read-only.", style="warn")
        text.append(
            " You can read its posts, and it won't keep yours. A room lets any password in "
            "read-only when its owner allows that, so this one may not be the room password."
        )
        buttons = [("Keep read-only", None), ("Try again…", _RETRY_PASSWORD)]
        return await ctx.ui.dialog(text, buttons, title=title, default=1)
    if login.result is LoginResult.REFUSED:
        text = Text(f"{room.name} refused the login.", style="warn")
        text.append(" The password may be wrong.")
        buttons = [("Cancel", None), ("Try again…", _RETRY_PASSWORD)]
        return await ctx.ui.dialog(text, buttons, title=title, default=1)
    lines = silence_explanation(ctx, room, login, password)
    text = Text(lines[0], style="warn")
    for line in lines[1:]:
        text.append(" " + line)
    if login.flood is None:
        missing = bool(login.radio_error) and "contacts" in login.radio_error
        step = ("Add it & try again", _RESTORE) if missing else ("Try again", _RETRY)
    elif not login.flood:
        # A learned route that worked before can fail silently; a flood goes round it.
        step = ("Try by flood", _RETRY_FLOOD)
    elif ctx.rooms.worked_before(room, password):
        step = ("Try again", _RETRY)
    else:
        step = ("Try again…", _RETRY_PASSWORD)
    return await ctx.ui.dialog(text, [("Cancel", None), step], title=title, default=1)


async def _restore(ctx: AppContext, room: Contact, *, asked: bool = False) -> bool:
    """Put a room the radio has forgotten back in its contacts; ``True`` once it is there.

    A login is addressed through the radio's own contact entry, so a room MeshTerm still
    lists but the radio has dropped can't be logged in to until it is written back — the
    same one write a direct message's "add it back" makes.
    """
    if not asked:
        add = await ctx.ui.dialog(
            f"{room.name} isn't in this radio's contacts, so it can't be addressed. "
            "Add it back and try again?",
            [("Cancel", False), ("Add & try again", True)],
            title=_title(ctx, room),
            default=1,
        )
        if not add:
            return False
    device = await ctx.device()
    await device.add_contact(room)
    ctx.devstate.invalidate_contacts()
    return True


# --- the page --------------------------------------------------------------------------


#: Width of the access lane (fits ``read-only``).
_ACCESS_WIDTH = 9
#: Width of the unread-badge lane (fits ``● 999``).
_BADGE_WIDTH = 5
#: Width of the age lanes (right-aligned; fits ``now`` and ``59m``).
_AGE_WIDTH = 5
#: The room name lane's floor and ceiling: as wide as the names in it, within these.
_NAME_MIN = 8
_NAME_MAX = 16


async def manage_rooms(ctx: AppContext) -> int:
    """Run the Rooms page until the reader backs out.

    Args:
        ctx: Shared application context (running the interactive TUI surface).

    Returns:
        How many logins the visit accepted — the run log's record, not anything shown.
    """
    joins = 0

    async def rows() -> tuple[str, list]:
        contacts = await ctx.devstate.contacts()
        return _page_items(ctx, [c for c in contacts if is_room(c.node_type)])

    title, items = await rows()
    menu = SelectScreen(title, items, footer_hint=_PAGE_HINT)
    async with ctx.ui.session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            if choice is CANCEL or choice is None:
                return joins
            joins += await _room_detail(ctx, str(choice))
            title, items = await rows()
            menu.replace_items(items, title=title)


def _page_items(ctx: AppContext, rooms: list[Contact]) -> tuple[str, list]:
    """The page's title and rows: the lane names, then Joined, then Not joined."""
    joined = sorted((r for r in rooms if ctx.rooms.joined(r)), key=lambda r: r.name.casefold())
    others = sorted(
        (r for r in rooms if not ctx.rooms.joined(r)),
        # Heard most recently first: the rooms in reach lead the ones long gone.
        key=lambda r: (r.last_seen is None, -(r.last_seen.timestamp() if r.last_seen else 0)),
    )
    peers = [_peer(r) for r in rooms]
    lasts = ctx.repo.last_chat_messages(rooms=peers)
    longest = max((cell_len(r.name) for r in rooms), default=0)
    name_w = max(_NAME_MIN, min(_NAME_MAX, longest))
    marker_w = cell_len(_ROOM_MARK[0]) + 1

    items: list = [Separator(lambda w: _header(marker_w, name_w, w), pinned=True)]
    items.append(section_heading("Joined"))
    for room in joined:
        items.append(_row(ctx, room, lasts, marker_w, name_w))
    if not joined:
        items.append(Separator("  no rooms joined yet — pick one below to join it"))
    items.append(section_heading("Not joined"))
    for room in others:
        items.append(_row(ctx, room, lasts, marker_w, name_w))
    if not others:
        items.append(Separator("  no other room servers heard yet"))
    return f"Rooms · {len(joined)} joined", items


def _peer(room: Contact) -> str:
    """The room's key as its conversation stores it."""
    return (room.key_prefix or room.public_key[:12] or "").lower()


def _header(marker_w: int, name_w: int, width: int) -> str:
    """Column headings over the page's lanes."""
    return column_header(
        [
            Lane("ROOM", name_w + 2),
            Lane("ACCESS", _ACCESS_WIDTH + 2),
            Lane("NEW", _BADGE_WIDTH + 2),
            Lane("LAST", _AGE_WIDTH + 2),
            Lane("HEARD"),
        ],
        width,
        indent=2 + marker_w,
    )


def _row(ctx: AppContext, room: Contact, lasts: dict, marker_w: int, name_w: int) -> Choice:
    """One room as fixed lanes: mark, name, access, unread, last post, and last heard."""

    def title() -> Text:
        conversation = Conversation(label=room.name, is_channel=False, contact=room)
        text = Text(no_wrap=True, overflow="ellipsis")
        mark, style = _ROOM_MARK
        text.append(mark, style=style)
        text.append(" " * max(1, marker_w - cell_len(mark)))
        text.append(fit_cells(room.name, name_w), style=name_style(room.name, room.public_key))
        text.append("  ")
        access = ctx.rooms.access(room) if ctx.rooms.joined(room) else None
        text.append(fit_cells(access.value if access else "", _ACCESS_WIDTH), style="muted")
        text.append("  ")
        unread = ctx.chat.unread(conversation.key)
        if unread:
            text.append("●", style="err")
            text.append(f" {unread}".ljust(_BADGE_WIDTH - 1), style="warn")
        else:
            text.append(" " * _BADGE_WIDTH)
        text.append("  ")
        last = lasts.get(conversation.key)
        posted = format_age(age_seconds(last.created_at)) if last is not None else ""
        text.append(f"{posted:>{_AGE_WIDTH}}", style="muted")
        text.append("  ")
        heard = format_age(age_seconds(room.last_seen)) if room.last_seen else "never"
        text.append(f"{heard:>{_AGE_WIDTH}}", style="muted")
        return text

    return Choice(title=title, value=room.public_key or room.key_prefix, fitted=True)


async def _room_detail(ctx: AppContext, key: str) -> int:
    """One room's page: its vital signs over its actions; returns logins accepted."""
    joins = 0

    async def current() -> Contact | None:
        contacts = await ctx.devstate.contacts()
        return next((c for c in contacts if (c.public_key or c.key_prefix) == key), None)

    room = await current()
    if room is None:  # pragma: no cover - the row came from this very cache
        return 0
    menu = SelectScreen(
        f"Room — {room.name}",
        _detail_items(ctx, room),
        prompt=_summary(ctx, room),
        footer_hint="↑↓ move · Enter select · Esc back",
    )
    async with ctx.ui.session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            if choice is CANCEL or choice is None:
                return joins
            if choice in (_JOIN, _RELOGIN, _PASSWORD):
                login = await join_room(ctx, room, ask=choice != _RELOGIN)
                if login is not None and login.access is not None:
                    joins += 1
            elif choice == _OPEN:
                from .room import open_room

                await open_room(ctx, Conversation(label=room.name, is_channel=False, contact=room))
            elif choice == _FORGET:
                await _forget(ctx, room)
            elif choice == _DELETE:
                await _delete_posts(ctx, room)
            room = await current() or room
            menu.replace_items(
                _detail_items(ctx, room), title=f"Room — {room.name}", prompt=_summary(ctx, room)
            )


def _summary(ctx: AppContext, room: Contact) -> str:
    """One line of vital signs, shed from the right until it fits the platform."""
    joined = ctx.rooms.joined(room)
    access = ctx.rooms.access(room) if joined else None
    parts = [access.value if access else ("joined" if joined else "not joined")]
    if room.last_seen is not None:
        ago = format_ago(age_seconds(room.last_seen))
        parts.append("heard just now" if ago == "now" else f"heard {ago}")
    else:
        parts.append("never heard")
    if room.route_hops is None:
        parts.append("no route — logins flood")
    else:
        hops = len(room.route_hops)
        parts.append(f"{hops}-hop route" if hops else "a neighbour")
    posts = len(ctx.repo.recent_chat_messages(is_channel=False, peer=_peer(room), posts_only=True))
    parts.append(f"{posts} post{'' if posts == 1 else 's'} stored" if posts else "no posts stored")
    # The page floats as a box, its border and padding eight cells inside the screen.
    width = get_platform().readable_cols - 8
    while len(parts) > 1 and cell_len(" · ".join(parts)) > width:
        parts.pop()
    return " · ".join(parts)


def _detail_items(ctx: AppContext, room: Contact) -> list:
    """The detail page's action rows, for a joined room or one not joined yet."""
    joined = ctx.rooms.joined(room)
    peer = _peer(room)
    stored = bool(
        ctx.repo.recent_chat_messages(is_channel=False, peer=peer, posts_only=True, limit=1)
    )
    lane = icon_lane(("💬", "↻", "🔑", "🗑"))
    unread = ctx.chat.unread(f"dm:{peer}")
    rows: list = []
    if joined:
        board = marked_label("💬", "Open the board", "", lane=lane)
        if unread:
            board.append("  ●", style="err")
            board.append(f" {unread}", style="warn")
        rows += [
            (board, "Read the posts, and post", _OPEN),
            (
                marked_label("↻", "Log in again", "", lane=lane),
                "Ask the room for what you missed",
                _RELOGIN,
            ),
            (
                marked_label("🔑", "Change password…", "", lane=lane),
                "Log in with a different password",
                _PASSWORD,
            ),
            (
                marked_label("🗑", "Forget this room…", "err", lane=lane),
                "Stop logging in, and forget its password",
                _FORGET,
            ),
        ]
    else:
        rows.append(
            (marked_label("🔑", "Join…", "", lane=lane), "Log in with the room's password", _JOIN)
        )
        if stored:
            rows.append(
                (
                    marked_label("💬", "Read stored posts", "", lane=lane),
                    "What it sent while you were in",
                    _OPEN,
                )
            )
    if stored:
        rows.append(
            (
                marked_label("🗑", "Delete stored posts…", "err", lane=lane),
                "Remove its posts from this machine",
                _DELETE,
            )
        )
    return menu_rows(rows)


async def _forget(ctx: AppContext, room: Contact) -> None:
    """Confirm, then leave a room as far as MeshTerm can (see ``RoomService.forget``)."""
    if await ctx.ui.dialog(
        f"Forget {room.name}? MeshTerm stops logging in to it and forgets its password. "
        "The room may go on sending your radio new posts until it restarts; the posts "
        "stored here are kept.",
        [("Cancel", False), ("Forget", True)],
        title=f"Forget — {room.name}",
        default=1,
        danger=True,
    ):
        ctx.rooms.forget(room)


async def _delete_posts(ctx: AppContext, room: Contact) -> None:
    """Confirm, then delete the room's stored posts (irreversible: the reserved red)."""
    if await ctx.ui.dialog(
        f"Delete the posts stored from {room.name}? They are removed from this machine, "
        "and the room won't send them again.",
        [("Cancel", False), ("Delete", True)],
        title="Delete posts",
        default=1,
        destructive=True,
    ):
        ctx.repo.delete_chat_history(_peer(room))
        ctx.chat.clear_unread(f"dm:{_peer(room)}")
