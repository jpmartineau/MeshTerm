# SPDX-License-Identifier: Apache-2.0
"""The admin-node picker: select a remote node for which you have (or will enter) credentials.

The tools that administer remote nodes over the mesh share this picker: TX optimize and
the repeater admin screen. Thus "select the node to manage" reads the same everywhere.
The nodes that have credentials are first in the list, in their own 🔑 section. These are
the nodes that the user administered before, so they are the most probable selections.
Repeaters and room servers follow, then all the other nodes. Each section is in order of
how recently the node was heard. MeshTerm offers each contact that has a public key,
because a password is a fact about the *user* and not about the node.

The picker is a **dialog**: a question that MeshTerm asks when the user goes into a tool.
It is drawn as a box, and it is gone when the user answers it. It is never a screen on the
stack under the page of the tool, where Esc would land. It was once kept pushed as a hub
for the whole visit. Then, when the user left the admin page, Esc landed on the list from
which the user selected the node. This looked like two places where there is one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text

from ..core.models import NODE_TYPE_REPEATER, NODE_TYPE_ROOM, Contact

if TYPE_CHECKING:
    from ..context import AppContext


def admin_picker_rows(ctx: AppContext, contacts: list[Contact]) -> tuple[list, list[Contact]]:
    """Build the grouped rows of the admin-node picker and the contacts that they map to.

    The function is shared so that each screen that offers the selection draws the same
    list. :func:`pick_admin_node` runs these rows as a one-shot dialog. TX optimize turns
    them as the first page of its dialog with steps. Each row has ``value = contact.name``,
    so a name that the user selects (or a ``default``) resolves through ``candidates``.

    Args:
        ctx: The shared application context (for the admin store, which sorts the
            remembered nodes to the top).
        contacts: The contacts that the device knows.

    Returns:
        ``(rows, candidates)``: the grouped select rows and the contacts that MeshTerm can
        offer (those that have a key). Both are empty when MeshTerm can offer nothing.
        The caller shows a note about this and does not open a list.
    """
    from .menus import section_heading
    from .theme import name_style
    from .tui import Choice
    from .widgets import DEFAULT_GLYPH, NODE_GLYPHS

    candidates = [c for c in contacts if (c.public_key or c.key_prefix).strip()]
    if not candidates:
        return [], candidates

    def row(contact: Contact) -> Choice:
        # The type mark keeps its own fixed hue. The *name* takes the hue that comes from
        # the key of the node, like each other list of nodes. (A style on the Text itself
        # would be the base of the row, and it would paint the name in the colour of the
        # type too.)
        glyph, glyph_style = NODE_GLYPHS.get(contact.node_type, DEFAULT_GLYPH)
        label = Text()
        label.append(f"{glyph} ", style=glyph_style)
        label.append(
            contact.name,
            style=name_style(contact.name, contact.public_key or contact.key_prefix),
        )
        return Choice(title=label, value=contact.name)

    def recency(contact: Contact) -> float:
        return -(contact.last_seen.timestamp() if contact.last_seen else 0.0)

    remembered = [c for c in candidates if ctx.admin_store.get(c) is not None]
    infrastructure = [
        c
        for c in candidates
        if c not in remembered and c.node_type in (NODE_TYPE_REPEATER, NODE_TYPE_ROOM)
    ]
    others = [c for c in candidates if c not in remembered and c not in infrastructure]

    items: list = []
    if remembered:
        items.append(section_heading("Remembered admins"))
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

    choice = await ctx.ui.select(
        title,
        items,
        prompt=prompt,
        default=default.name if default is not None else None,
        floating=True,
    )
    return _resolve(candidates, choice)


async def _note_nothing_to_pick(ctx: AppContext, title: str) -> None:
    """Say that no node can be offered, and show this. MeshTerm opens no list."""
    ctx.ui.note("[err]no contacts with a key — receive an advert first[/err]")
    await ctx.ui.present(title=title)


def _resolve(candidates: list[Contact], choice: Any) -> Contact | None:
    """The contact for the name of a selected row, or ``None`` if the user selected nothing."""
    if choice is None:
        return None
    return next((c for c in candidates if c.name == choice), None)
