# SPDX-License-Identifier: Apache-2.0
"""The Archived contacts list: what the sweep took off the device, and when.

An archive sweep (refer to :mod:`~meshterm.ui.sweep_screen`) removes a contact from the
*device*, which is the scarce resource, and keeps the contact here in full. This screen is
where those contacts are. It has the same sortable and filterable lane layout as the
Contacts list, with its own lane set (``NAME · ARCHIVED · KEY``,
:data:`~meshterm.ui.contactlist.ARCHIVED_LANES`) and its own sort ring over exactly those
three columns.

The ``ARCHIVED`` lane is drawn in the same way as ``HEARD`` on the main list: a relative
age in the recency heat colours. It answers the same type of question about a different
event. A second grammar for "how long ago" would give the user something more to learn,
with no benefit. The screen opens sorted by this lane, with the newest first. The question
that this screen answers is *what did that sweep just take?*, and the answer belongs at
the top.

**It has no actions.** It is not a select list with the verbs removed. It is a list with
nothing to select, and Esc leaves it. Each action that you can do to an archived contact
belongs to that one contact. The actions are on its Node detail page, which Enter opens:
restore it to the device, or delete it for good. A restore verb on this screen also would
be a second route to the same write. The page is the place where the user can see which
node the action is for.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .contactlist import (
    ARCHIVED_LANES,
    ARCHIVED_SORT_COLUMNS,
    ARCHIVED_SORT_OPENS_ASCENDING,
    ContactListScreen,
    ContactRow,
)
from .tui.screen import CANCEL
from .widgets import ContactsSort

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.contact_store import RememberedContact

#: The footer of the list: navigation, the sort, the filter, then Esc last. The hint has
#: ``Enter open`` because a row pushes the detail page of the node. The Contacts list uses
#: the same verb for the same gesture.
_HINT = "↑↓ move · ^←→↑↓ sort · type to filter · Enter open · Esc back"


def _archived_time(stamp: int | None) -> datetime | None:
    """A stored archive stamp as a UTC datetime with a time zone, or ``None`` if it has none.

    MeshTerm stores the stamp as unix seconds (refer to
    :class:`~meshterm.core.contact_store.RememberedContact`), and the lane renders it as an
    age in local time. The function changes it to the unit of the list here, and not in
    three places further down.
    """
    if not stamp:
        return None
    try:
        return datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None  # a stamp that is not valid reads "never", and does not crash


def archived_rows(archived: list[RememberedContact]) -> list[ContactRow]:
    """Change remembered contacts to row data for the shared list.

    Args:
        archived: The archived contacts of the device, from
            :meth:`~meshterm.core.contact_store.ContactStore.archived`.

    Returns:
        One :class:`~meshterm.ui.contactlist.ContactRow` for each contact. The row has the
        rebuilt :class:`~meshterm.core.models.Contact` as its value, so that Enter gives
        the whole record to the detail page with no new search.
    """
    return [
        ContactRow(
            value=remembered.to_contact(),
            name=remembered.name,
            key=remembered.public_key,
            node_type=remembered.node_type,
            archived_at=_archived_time(remembered.archived_at),
        )
        for remembered in archived
    ]


class ArchivedScreen(ContactListScreen):
    """A full-screen, sortable list of the contacts that a sweep took off this device."""

    def __init__(self, rows: list[ContactRow], prefix_bytes: int, sort: ContactsSort) -> None:
        """Build the list over rows that MeshTerm already resolved.

        Args:
            rows: The lane data of the archived contacts (refer to :func:`archived_rows`).
            prefix_bytes: The path hash width in bytes to highlight in each key.
            sort: The sort. The Ctrl+arrows change it in place. Its ring should span
                :data:`~meshterm.ui.contactlist.ARCHIVED_SORT_COLUMNS`.
        """
        super().__init__(
            f"Archived contacts · {len(rows)} kept",
            rows=rows,
            prefix_bytes=prefix_bytes,
            sort=sort,
            prompt=("Off the device, kept here with their history. Open one to restore it."),
            footer_hint=_HINT,
            lanes=ARCHIVED_LANES,
        )


async def open_archived(ctx: AppContext, self_key: str, prefix_bytes: int) -> bool:
    """Open the archived list. Enter opens Node detail, and Esc leaves.

    The function returns ``True`` if anything changed.

    The list stays pushed for the whole visit. Thus a detail page nests above it, and Esc
    from the page lands back on the row from which the user opened it, with the same sort,
    the same filter, and the same scroll. A page can *restore* or *delete* the contact that
    it shows. Then the visit ends and MeshTerm builds the list again without the row. This
    is the same deliberate exception that the Contacts list makes: the row in which the
    screen held a place does not exist now.

    Args:
        ctx: The shared application context (interactive menu).
        self_key: The own public key of the device (hex). The store uses it to scope the
            remembered contacts of this device.
        prefix_bytes: The path hash width in bytes to highlight in each key.

    Returns:
        ``True`` if the user restored or deleted any contact, so that the contact list of
        the caller knows that it must read again.
    """
    from .node_detail_screen import open_node_detail
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # the Contacts screen makes sure of this
    session = ctx.ui.session
    store = ctx.contact_store
    dev_pub = (self_key or "").lower().removeprefix("0x")
    sort = ContactsSort.from_name("archived", ARCHIVED_SORT_COLUMNS, ARCHIVED_SORT_OPENS_ASCENDING)
    changed = False
    while True:
        rows = archived_rows(store.archived(dev_pub) if store and dev_pub else [])
        if not rows:
            # The user restored or deleted each contact while in here. No list is left to
            # show. An empty screen that the user must leave with Esc says less than the
            # note that MeshTerm shows when the user goes back.
            ctx.ui.note("[muted]no archived contacts[/muted]")
            return changed
        screen = ArchivedScreen(rows, prefix_bytes, sort)
        rebuild = False
        async with session.stay(screen) as visit:
            while True:
                chosen = await visit.result()
                if chosen is CANCEL or chosen is None:  # Esc
                    return changed
                # The page manages the contact that it shows (restore it, or delete it for
                # good). It says so when it closes, and then this list is stale.
                if await open_node_detail(ctx, chosen):
                    changed = rebuild = True
                    break
        if not rebuild:
            return changed
