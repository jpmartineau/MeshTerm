# SPDX-License-Identifier: Apache-2.0
"""The Rooms page: the room servers that your radio knows, and how to join them.

This page is the counterpart of the Channels page for rooms (refer to
:mod:`meshterm.ui.channels`). A channel is something that the radio holds in a slot. A room
is a place where you log in. Channels creates slots and gives them keys. This page lists
each room server that the radio knows, with the joined rooms first, and it joins them. To
join a room, the user gives a password, MeshTerm makes one login, and the room says what it
lets us do. After the user joins a room, the room is in Chat, the same as a channel after
it has a slot. The user reads the board of the room, and posts to it, there
(:mod:`meshterm.ui.room`).

Joining is also the place where a room is hardest to understand, because a room never says
*no*. A wrong password gets no reply at all. A room that is out of reach gives no reply.
A login that goes along a route that the radio learned, and that is now stale, gives no
reply. "No reply" alone does not help. Thus the page explains the result from what MeshTerm
really knows: how the login went out, when the room was last heard, and whether this
password worked before. The page then offers the next step as buttons: try by flood (which
first forgets the stale route), try again, or try another password. Each of these is one
more login that the user asked for. MeshTerm never sends one on its own.

This flow (:func:`join_room`) is shared. The ^L key of the board also runs it. The
``rooms join`` command of the command line explains a silent room in the same words
(:func:`silence_explanation`).

The page has the shape of the Channels page. One list stays pushed for the whole visit.
Each room has a detail page, with its vital signs above its actions. Each prompt floats
over the page.
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

#: The footer sentence of the page: navigation, then the filter, then the action, and Esc
#: last.
_PAGE_HINT = "↑↓ move · type to filter · Enter open · Esc back"

#: MeshCore copies a maximum of this number of bytes of a login password into the request
#: (``BaseChatMesh::sendLogin``). It drops the rest with no message. Thus a longer password
#: can never work. The prompt refuses it, so that the user does not get silence on the air.
PASSWORD_BYTES = 15

# The sentinels of the actions of the detail page.
_OPEN = "open"
_JOIN = "join"
_RELOGIN = "relogin"
_PASSWORD = "password"
_FORGET = "forget"
_DELETE = "delete"

# The values that the buttons of a result dialog return.
_RETRY_FLOOD = "flood"
_RETRY_PASSWORD = "password"
_RETRY = "again"
_RESTORE = "restore"

#: The marker of a room row: the ``■`` of the room server, in its type colour (the same as
#: on the map, in the contact list, and in the chat picker).
_ROOM_MARK = NODE_GLYPHS[NODE_TYPE_ROOM]


# --- joining ---------------------------------------------------------------------------


def valid_password(text: str) -> bool | str:
    """Check that a login can carry the whole password: :data:`PASSWORD_BYTES` bytes at most."""
    if len(text.encode("utf-8")) > PASSWORD_BYTES:
        return f"MeshCore sends at most {PASSWORD_BYTES} characters of a password"
    return True


async def ask_password(ctx: AppContext, room: Contact, default: str = "") -> str | None:
    """Ask for the password of ``room``. Return ``None`` if the user backs out.

    A blank answer is a real answer. It asks the room if it already knows us. A room knows
    its admins, and each user who logged in after the room last restarted. ``default``
    fills the field (masked) with the password that the user just tried. Thus to try it
    again, the user presses Enter, and to try another password, the user types over it.
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
    """The reason why a login got no answer, from what MeshTerm knows, in one sentence each.

    This is the only explanation. The join dialogs and the command line share it. A room
    that does not answer can have missed the login, or can have heard it with the wrong
    password. It says nothing in both cases. Thus the function gives the facts that show the
    difference, and not a guess. The facts are: how the radio sent the login (a stale
    learned route is the most common silent failure, and a flood is the way around it),
    when the room was last heard, and whether this password ever worked here.

    Args:
        ctx: The shared application context.
        room: The room, as the contact cache of the session holds it (its ``last_seen`` is
            the later of its own advert and our last reception of it).
        login: The login that got no answer.
        password: The password that the login carried.

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
    """Join ``room``, or log in to it again, with a full explanation.

    This is the only explicit flow. The flow uses the password that is remembered for the
    room, unless ``ask`` is true or there is no remembered password. In these cases, the
    flow asks the user. The login runs
    under a modal card that counts the seconds, because a room that is several hops away can
    take fifteen seconds to answer. If the login lets the user post, the flow ends quietly.
    The page or the board from which the flow started shows the access. For each other
    result, the flow gives an explanation (:func:`_outcome`), and the user selects what to
    try next. Each retry is one more login that the user chose to send.

    Args:
        ctx: The shared application context (which runs the interactive TUI surface).
        room: The room server, as the contact cache of the session holds it.
        ask: Ask for the password also when one is remembered.

    Returns:
        The result of the last login, or ``None`` if the user backed out before MeshTerm
        sent a login.
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
        except Exception as exc:  # noqa: BLE001 - the flow says each failure. None is fatal.
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
    """The title of the dialogs of the flow: ``Join — Room``, or ``Log in — Room`` if joined."""
    return f"{'Log in' if ctx.rooms.joined(room) else 'Join'} — {room.name}"


async def _log_in(ctx: AppContext, room: Contact, password: str, *, flood: bool) -> RoomLogin:
    """Send one login under a modal card that says how it went out and counts the wait.

    The card has two lines, on purpose. The first line has the room. The second line has
    how the login went out and the seconds that it has waited. Thus a long name never
    pushes the count off the end of the box, and the count changes in the same place each
    second.
    """
    if flood or room.route_hops is None:
        how = "by flood"
    elif room.route_hops:
        how = f"along its {len(room.route_hops)}-hop route"
    else:
        how = "directly"
    caption = f"logging in to {room.name}\n{how}…"
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
    """Explain a login that did not let us post, and ask what to try next.

    The dialog has a maximum of two buttons: the safe way out, and the one next step that
    the evidence shows. Thus the row fits the 45-cell dialog of the handhelds whole. A stale
    route is the first thing to rule out. Its silence is the most common, and it costs the
    least to test: the same password by flood. The password is in question only after a
    flood got no answer. Then *Try again…* opens the prompt again with the password filled
    in, so Enter sends it again and typing replaces it.

    Returns:
        The value of the button that the user selected: one of the ``_RETRY*`` or
        ``_RESTORE`` sentinels, or ``None`` to stop.
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
        # A learned route that worked before can fail silently. A flood goes around it.
        step = ("Try by flood", _RETRY_FLOOD)
    elif ctx.rooms.worked_before(room, password):
        step = ("Try again", _RETRY)
    else:
        step = ("Try again…", _RETRY_PASSWORD)
    return await ctx.ui.dialog(text, [("Cancel", None), step], title=title, default=1)


async def _restore(ctx: AppContext, room: Contact, *, asked: bool = False) -> bool:
    """Put a room that the radio forgot back in its contacts. Return ``True`` when it is there.

    MeshTerm addresses a login through the radio's own contact entry. Thus if MeshTerm
    still lists a room but the radio dropped it, the user cannot log in to the room until
    MeshTerm writes it back. This is the same one write that "add it back" makes for a
    direct message.
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


#: The width of the access lane (it fits ``read-only``).
_ACCESS_WIDTH = 9
#: The width of the lane of the unread badge (it fits ``● 999``).
_BADGE_WIDTH = 5
#: The width of the age lanes (aligned to the right, and they fit ``now`` and ``59m``).
_AGE_WIDTH = 5
#: The minimum and the maximum width of the lane of the room name. The lane is as wide as
#: the names in it, within these limits.
_NAME_MIN = 8
_NAME_MAX = 16


async def manage_rooms(ctx: AppContext) -> int:
    """Run the Rooms page until the user backs out.

    Args:
        ctx: The shared application context (which runs the interactive TUI surface).

    Returns:
        The number of logins that the rooms accepted in the visit. This is for the run log.
        MeshTerm does not show it.
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
    """The title and the rows of the page: the lane names, then Joined, then Not joined."""
    joined = sorted((r for r in rooms if ctx.rooms.joined(r)), key=lambda r: r.name.casefold())
    others = sorted(
        (r for r in rooms if not ctx.rooms.joined(r)),
        # The room that was heard most recently is first. Thus the rooms in reach are before
        # the rooms that went away long ago.
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
    """The key of the room as its conversation stores it."""
    return (room.key_prefix or room.public_key[:12] or "").lower()


def _header(marker_w: int, name_w: int, width: int) -> str:
    """The column headings above the lanes of the page."""
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
    """One room as fixed lanes: mark, name, access, unread, last post, and last heard time."""

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
    """The page of one room: its vital signs above its actions. Return the logins accepted."""
    joins = 0

    async def current() -> Contact | None:
        contacts = await ctx.devstate.contacts()
        return next((c for c in contacts if (c.public_key or c.key_prefix) == key), None)

    room = await current()
    if room is None:  # pragma: no cover - the row came from this cache
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
    """One line of vital signs. MeshTerm removes atoms from the right until it fits the platform."""
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
    # The page floats as a box. Its border and padding take eight cells of the screen.
    width = get_platform().readable_cols - 8
    while len(parts) > 1 and cell_len(" · ".join(parts)) > width:
        parts.pop()
    return " · ".join(parts)


def _detail_items(ctx: AppContext, room: Contact) -> list:
    """The action rows of the detail page, for a joined room or a room that is not joined."""
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
    """Ask for a confirmation, then leave a room as far as MeshTerm can.

    Refer to ``RoomService.forget``.
    """
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
    """Ask for a confirmation, then delete the stored posts of the room.

    This cannot be undone, so the dialog has the reserved red.
    """
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
