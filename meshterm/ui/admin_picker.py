# SPDX-License-Identifier: Apache-2.0
"""The admin-node picker: select a remote node for which you have (or will enter) credentials.

The tools that administer remote nodes over the mesh share this picker: TX optimize and
the repeater admin screen. Thus "select the node to manage" reads the same everywhere.
The nodes that have credentials are first in the list, in their own section: the *managed
nodes*, each with the ``🔑`` of :data:`~meshterm.ui.marks.MANAGED_ICON`. These are the nodes
that the user administered before, so they are the most probable selections.
Repeaters and room servers follow, then all the other nodes. Each section is in order of
how recently the node was heard. MeshTerm offers each contact that has a public key,
because a password is a fact about the *user* and not about the node.

The picker is a **dialog**: a question that MeshTerm asks when the user goes into a tool.
It is drawn as a box, and it is gone when the user answers it. It is never a screen on the
stack under the page of the tool, where Esc would land. It was once kept pushed as a hub
for the whole visit. Then, when the user left the admin page, Esc landed on the list from
which the user selected the node. This looked like two places where there is one.

Delete on a managed node forgets its password, after a confirm (``Del forget``). The
``Forget passwords`` row of the menu forgets all of them, after a typed confirm (refer to
:func:`confirm_forget_all`). These are the two places in the menu where the user can make
MeshTerm forget an admin password. A stock node does not answer a wrong password, so
MeshTerm cannot find out by itself that a stored password is stale (GitHub issue 28, and
refer to :mod:`meshterm.ui.admin_login`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text

from ..core.models import NODE_TYPE_REPEATER, NODE_TYPE_ROOM, Contact

if TYPE_CHECKING:
    from ..context import AppContext

#: The footer atom of the Delete key on a managed node.
FORGET_HINT = "Del forget"

#: The word that the user types to forget all the stored admin passwords.
FORGET_ALL_WORD = "FORGET"

#: The most node names that the confirm to forget all the passwords shows. It counts the
#: other nodes.
_NAMES_SHOWN = 5


def admin_picker_rows(ctx: AppContext, contacts: list[Contact]) -> tuple[list, list[Contact]]:
    """Build the grouped rows of the admin-node picker and the contacts that they map to.

    The function is shared so that each screen that offers the selection draws the same
    list. :func:`pick_admin_node` runs these rows as a one-shot dialog. TX optimize turns
    them as the first page of its dialog with steps. Each row has ``value = contact.name``,
    so a name that the user selects (or a ``default``) resolves through ``candidates``.
    The rows of the managed nodes are :attr:`~meshterm.ui.tui.select.Choice.deletable`:
    Delete on one of them forgets its password (refer to :func:`forget_row`).

    Args:
        ctx: The shared application context (for the admin store, which sorts the
            remembered nodes to the top).
        contacts: The contacts that the device knows.

    Returns:
        ``(rows, candidates)``: the grouped select rows and the contacts that MeshTerm can
        offer (those that have a key). Both are empty when MeshTerm can offer nothing.
        The caller shows a note about this and does not open a list.
    """
    from rich.cells import cell_len

    from .marks import MANAGED_ICON
    from .menus import section_heading
    from .theme import glyph as platform_glyph
    from .theme import name_style
    from .tui import Choice
    from .widgets import DEFAULT_GLYPH, NODE_GLYPHS

    candidates = _candidates(contacts)
    if not candidates:
        return [], candidates
    remembered = [c for c in candidates if ctx.admin_store.get(c) is not None]
    # The mark column, as in the Contacts list: the key of a managed node, or blanks of the
    # same width, so that each type mark stays in one column. No cells when no node is
    # managed.
    key_mark = platform_glyph(MANAGED_ICON)
    mark_w = cell_len(key_mark) + 1 if remembered else 0

    def row(contact: Contact) -> Choice:
        # The type mark keeps its own fixed hue. The *name* takes the hue that comes from
        # the key of the node, like each other list of nodes. (A style on the Text itself
        # would be the base of the row, and it would paint the name in the colour of the
        # type too.)
        glyph, glyph_style = NODE_GLYPHS.get(contact.node_type, DEFAULT_GLYPH)
        label = Text()
        if mark_w:
            label.append(f"{key_mark} " if contact in remembered else " " * mark_w)
        label.append(f"{glyph} ", style=glyph_style)
        label.append(
            contact.name,
            style=name_style(contact.name, contact.public_key or contact.key_prefix),
        )
        return Choice(title=label, value=contact.name, deletable=contact in remembered)

    def recency(contact: Contact) -> float:
        return -(contact.last_seen.timestamp() if contact.last_seen else 0.0)

    infrastructure = [
        c
        for c in candidates
        if c not in remembered and c.node_type in (NODE_TYPE_REPEATER, NODE_TYPE_ROOM)
    ]
    others = [c for c in candidates if c not in remembered and c not in infrastructure]

    items: list = []
    if remembered:
        items.append(section_heading("Managed nodes"))
        items.extend(row(c) for c in sorted(remembered, key=recency))
    if infrastructure:
        items.append(section_heading("Repeaters & rooms"))
        items.extend(row(c) for c in sorted(infrastructure, key=recency))
    if others:
        items.append(section_heading("Other contacts"))
        items.extend(row(c) for c in sorted(others, key=recency))
    return items, candidates


async def pick_admin_node(
    ctx: AppContext,
    contacts: list[Contact],
    *,
    title: str,
    prompt: str,
    default: Contact | None = None,
) -> Contact | None:
    """Select a remote node to administer.

    Nodes that have credentials, and infrastructure nodes, are first in the list. The
    picker is a floating dialog that closes when the user selects a node. A caller can ask
    again, for example when the login that it tried was refused. Then the caller
    returns the last selection as ``default``, so that the list opens on it again. A step
    of :func:`~meshterm.ui.menus.run_steps` offers its previous answer in the same way.

    Args:
        ctx: The shared application context (for the UI surface and the admin store).
        contacts: The contacts that the device knows.
        title: The heading of the select screen (it names the feature that calls it).
        prompt: One line above the list that says what the selection is for.
        default: The node on which the list opens highlighted, if any.

    Returns:
        The contact that the user selected, or ``None`` if the user cancels (or if there is
        nothing to select).
    """
    items, candidates = admin_picker_rows(ctx, contacts)
    if not candidates:
        await _note_nothing_to_pick(ctx, title)
        return None

    from .tui import CANCEL, SelectScreen
    from .tui.select import DeleteRequest

    screen = SelectScreen(
        title,
        items,
        prompt=prompt,
        default=default.name if default is not None else None,
        delete_hint=FORGET_HINT,
    )
    # The box stays pushed while the user forgets passwords in it, so the confirm floats
    # over the list, and the list refreshes in place. It is popped before the caller
    # gets the node, as a one-shot dialog is.
    async with ctx.ui.session.stay(screen, dialog=True) as visit:
        while True:
            choice = await visit.result()
            if isinstance(choice, DeleteRequest):
                rows = await forget_row(ctx, contacts, choice.value)
                if rows is not None:
                    screen.replace_items(rows)
                continue
            return None if choice is CANCEL else _resolve(candidates, choice)


async def forget_password(ctx: AppContext, node: Contact) -> bool:
    """Ask for a confirmation, then forget the stored admin password of ``node``.

    The confirm is amber, as the confirm to forget a room is: the next login asks for the
    password again, and a user who does not know it any more cannot log in.

    Args:
        ctx: The shared application context (for the dialog and the admin store).
        node: The managed node.

    Returns:
        ``True`` when MeshTerm forgot the password.
    """
    if not await ctx.ui.dialog(
        f"Forget the admin password of {node.name}? The next login asks for it again.",
        [("Cancel", False), ("Forget", True)],
        title=f"Forget password — {node.name}",
        default=1,
        danger=True,
    ):
        return False
    ctx.admin_store.forget(node)
    return True


async def forget_row(ctx: AppContext, contacts: list[Contact], value: Any) -> list | None:
    """Forget the password of the node on a picker row, and return the new rows.

    This is what Delete does on a row of :func:`admin_picker_rows`, in each list that
    shows those rows (:func:`pick_admin_node`, and the first page of the TX optimize
    dialog). The node moves out of the managed section, so the caller replaces the rows.

    Args:
        ctx: The shared application context.
        contacts: The contacts that the rows were built from.
        value: The value of the row (the name of the contact).

    Returns:
        The new rows, or ``None`` when nothing changed (the user cancelled, or the row
        names no node).
    """
    node = _resolve(_candidates(contacts), value)
    if node is None or not await forget_password(ctx, node):
        return None
    return admin_picker_rows(ctx, contacts)[0]


async def confirm_forget_all(ctx: AppContext, *, title: str) -> bool:
    """Ask the user to type a word before MeshTerm forgets all the stored admin passwords.

    This is a bulk deletion that the user cannot undo, so the confirm is the red typed
    confirm, not a button dialog. It names the nodes, because the store also holds the
    password of a node that the device does not know any more, and no list shows that
    node. When the store holds no password, the function says so, and asks nothing.

    Args:
        ctx: The shared application context (for the dialogs and the admin store).
        title: The title of the dialogs (the name of the menu row).

    Returns:
        ``True`` when the user typed the word. Then the caller forgets the passwords.
    """
    stored = ctx.admin_store.credentials()
    if not stored:
        await ctx.ui.session.message_dialog(
            Text("no saved admin passwords — MeshTerm saves one when a login works", "muted"),
            title=title,
        )
        return False
    names = [c.label or c.key[:12] for c in stored]
    if len(names) > _NAMES_SHOWN:
        names = [*names[:_NAMES_SHOWN], f"{len(names) - _NAMES_SHOWN} more"]
    if len(stored) == 1:
        warning = (
            f"This forgets the saved admin password of {names[0]}. "
            "The next login asks for it again."
        )
    else:
        listed = (
            " and ".join(names) if len(names) == 2 else f"{', '.join(names[:-1])}, and {names[-1]}"
        )
        warning = (
            f"This forgets the saved admin passwords of {len(stored)} managed nodes: "
            f"{listed}. Each next login asks for the password again."
        )
    return await ctx.ui.typed_confirm(warning, FORGET_ALL_WORD, title=title)


async def _note_nothing_to_pick(ctx: AppContext, title: str) -> None:
    """Say that no node can be offered, and show this. MeshTerm opens no list."""
    ctx.ui.note("[err]no contacts with a key — receive an advert first[/err]")
    await ctx.ui.present(title=title)


def _candidates(contacts: list[Contact]) -> list[Contact]:
    """The contacts that the picker can offer: those that have a key."""
    return [c for c in contacts if (c.public_key or c.key_prefix).strip()]


def _resolve(candidates: list[Contact], choice: Any) -> Contact | None:
    """The contact for the name of a selected row, or ``None`` if the user selected nothing."""
    if choice is None:
        return None
    return next((c for c in candidates if c.name == choice), None)
