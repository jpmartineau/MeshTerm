# SPDX-License-Identifier: Apache-2.0
"""The interactive Contacts screen: the contacts of the device in the shared sortable list.

This screen shows the contact table of the companion with the presentation of the Time
Machine picker. It has the same aligned ``NAME · HEARD · PKTS · KEY`` lanes, with our node
pinned first. It has the same Ctrl+arrow sort (now with the key column) and the same
type-to-filter. Thus the whole app has one contact list, for each source of data (refer to
:mod:`~meshterm.ui.contactlist`). Enter opens the full-screen Node detail page of the
highlighted node (:mod:`~meshterm.ui.node_detail_screen`). That page shows the identity of
the node, a preview of its location, the routes that MeshTerm observed to it, and the ways
to go further (trace, map, Time Machine). When the user backs out of that page, the list is
as it was. The one-shot CLI (``meshterm contacts --sort …``) still renders the static
:func:`~meshterm.ui.widgets.contacts_table`. Only the menu gets the live list.

Below the contacts is the maintenance action of the list: **Archive contacts**. It is the
bulk counterpart of the single archive action. It exists because the contact table of a
companion is finite. A busy mesh fills the table with nodes that were heard one time, in
passing, until no room is left to discover a new node. The action ranks the whole table
(refer to :mod:`~meshterm.core.contact_score`). It archives the weakest contacts off the
device, and MeshTerm keeps them. The flow, its ladder, and its preview are in
:mod:`~meshterm.ui.sweep_screen`. Our node is pinned above the list and is never a
candidate, because it is not in the contact table of the device.

MeshTerm never hides what a sweep took. **View archived contacts** is directly under the
archive action. It opens the archived list (refer to :mod:`~meshterm.ui.archived_screen`).
This is its own screen, with the same lane layout, sorted on its own ``NAME · ARCHIVED ·
KEY`` columns. It is a screen and not a section at the end of this screen, because it is a
different list with different columns. An archived contact has no live heard age or packet
count that is worth a lane. What it does have, the time that it left, has no column here.

To remove *one* named contact for good belongs to that contact and not to the list. It is
the last action on the Node detail page of the contact, where the user already looks at
the node that the user wants to remove. That action is a real deletion. It goes to both
halves of what the list shows: the own table of the device and the store that lasts
between sessions (refer to :mod:`~meshterm.core.contact_store`). Thus a bridge with no
firmware does not merge the contact back. In both cases, only the *contact* goes. MeshTerm
does not change the reception history and the overheard traffic of each node.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .archived_screen import open_archived
from .contactlist import ContactListScreen, ContactRow
from .menus import icon_lane, marked_label
from .sweep_screen import archive_contacts
from .tui import Choice, Separator
from .tui.screen import CANCEL
from .widgets import ContactsSort, contact_packets

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.models import Contact

#: A stable identity for the row of our node. The lane data of this row has no key when no
#: device answered. Enter on this row opens the detail page of our node.
YOU = ("you",)

#: The sentinels of the tail of the contact list (different from each
#: :class:`~meshterm.core.models.Contact` and from :data:`YOU`): the sweep, and the way to
#: what it already took.
_ARCHIVE = ("archive",)
_ARCHIVED = ("archived",)

#: Each mark that a maintenance row of the tail starts with. MeshTerm measures the icon
#: column over this set (refer to :func:`~meshterm.ui.menus.icon_lane`). The set is
#: declared and not found from the rows. Thus the column has one width for the whole tail,
#: for each of the two rows that a visit draws.
_TAIL_ICONS = ("💾", "📂")


class ContactsScreen(ContactListScreen):
    """A full-screen list of our node and its known contacts, sortable with Ctrl+arrow.

    Enter resolves the highlighted row, our node (:data:`YOU`) or a
    :class:`~meshterm.core.models.Contact`, so that :func:`open_contacts` opens it in the
    Node detail page. The shared list handles the sort, the filter, and the navigation.
    """

    def __init__(
        self,
        self_name: str,
        self_key: str,
        contacts: list[Contact],
        prefix_bytes: int,
        counts: dict[str, int],
        sort: ContactsSort,
        archived: int = 0,
        locked: frozenset[str] = frozenset(),
    ) -> None:
        """Create the contacts screen over contact data that MeshTerm already got.

        Args:
            self_name: The advertised name of our node.
            self_key: The full public key of our node (hex). If it is blank, the screen
                draws a muted ``?``.
            contacts: The known contacts to list under our node.
            prefix_bytes: The path hash width in bytes to highlight in each key.
            counts: The counts of overheard packets, keyed by the node id in lowercase (12
                hex digits). A contact with no entry shows a faint ``—``.
            sort: The initial sort. The Ctrl+arrows change it in place. Its ring should
                span :data:`~meshterm.ui.contactlist.SORT_COLUMNS` so that the hash sort
                is available.
            archived: The number of contacts that are archived off this device. If it is
                not zero, the screen adds the ``View archived contacts`` row and shows the
                count on that row and in the title (``Contacts · 198 known + 123
                archived``). Thus the user sees the count without opening the list. If it
                is zero, the screen draws no row and the title ends at ``… known``,
                because a screen must not offer a way into an empty list.
            locked: The full keys (lowercase hex) of the contacts that are locked against
                archiving. Each of those rows ends its name lane with a padlock.
        """
        self._self_name, self._self_key = self_name, self_key
        self._contacts, self._counts = contacts, counts
        rows = self._contact_rows_for(locked)
        # The maintenance actions close the list, after each contact, for each sort. First
        # a blank spacer, then the sweep (it has a `…` because it opens more prompts), then
        # the way to what the sweep already took. The two rows are together because they are
        # the two halves of one idea. The archived row is *under* the sweep for the same
        # reason: it is where the output of the sweep went. The sweep has the 💾 of the
        # single-contact Archive action and no red, because it archives, and we keep red for
        # data loss. Both rows have one icon column. MeshTerm measures it and does not
        # assume it. Thus a mark that is narrower than the mark of the other row cannot
        # start its label one column to the left of the label of the other row.
        lane = icon_lane(_TAIL_ICONS)
        tail: list = []
        if contacts:
            tail = [
                Separator(" "),
                Choice(
                    title=marked_label("💾", "Archive contacts…", "", lane=lane),
                    value=_ARCHIVE,
                ),
            ]
        if archived:
            tail = tail or [Separator(" ")]
            # The count is on the row in the muted style, as a status atom and not as part
            # of the verb. It is what the row leads to, not what the row does.
            label = marked_label("📂", "View archived contacts", "", lane=lane)
            label.append(f"  ·  {archived}", style="muted")
            tail.append(Choice(title=label, value=_ARCHIVED))
        # The title counts both halves of the contacts of this device: those that are on it
        # and those that a sweep moved off it. If the frame is too narrow for the words, the
        # numbers stay and the words go (``Contacts · 198 + 123``).
        known = len(contacts)
        title = f"Contacts · {known} known"
        if archived:
            title += f" + {archived} archived"
        super().__init__(
            title,
            rows=rows,
            prefix_bytes=prefix_bytes,
            sort=sort,
            tail=tail,
        )
        if archived:
            self.short_title = f"Contacts · {known} + {archived}"

    def _contact_rows_for(self, locked: frozenset[str]) -> list[ContactRow]:
        """Our node, then each contact. ``locked`` shows which contacts are locked."""
        rows = [ContactRow(value=YOU, name=self._self_name, key=self._self_key, you=True)]
        for c in self._contacts:
            rows.append(
                ContactRow(
                    # The contact itself is the identity of the row. Thus Enter gives the
                    # whole record to the detail page, with no new search by name or key.
                    value=c,
                    name=c.name,
                    key=c.public_key,
                    node_type=c.node_type,
                    last_seen=c.last_seen,
                    count=contact_packets(c, self._counts),
                    locked=(c.public_key or "").lower().removeprefix("0x") in locked,
                )
            )
        return rows

    def refresh_locks(self, locked: frozenset[str]) -> None:
        """Draw the padlocks again in place, after a detail page can lock or unlock a contact.

        A lock changes a lane, but it does not change which contacts exist. Thus this is the
        case for :meth:`~meshterm.ui.contactlist.ContactListScreen.update_rows`, not for a
        new screen. The highlight, the sort, the scroll, and a typed filter all stay where
        they were.
        """
        self.update_rows(self._contact_rows_for(locked))


async def open_contacts(
    ctx: AppContext,
    self_name: str,
    self_key: str,
    contacts: list[Contact],
    prefix_bytes: int,
    counts: dict[str, int],
    sort: ContactsSort,
) -> None:
    """Open the interactive contacts list. Enter opens Node detail, and Esc leaves.

    The list stays pushed for the whole visit. Thus the detail page of the node that Enter
    selects, the archive flow, and the archived list (the tail actions open the last two)
    nest *above* it. Esc from there is one pop back onto the row from which the user opened
    the page, with the same sort, the same filter, and the same scroll. Some actions change
    which contacts the device holds: a sweep here, a single archive, restore, or delete on
    a detail page, or a restore from inside the archived list. These actions end the visit.
    Then MeshTerm reads the device again and starts a new visit. This new screen is the
    deliberate exception to keeping the screen: the rows that the screen held a place in do
    not exist now.

    Args:
        ctx: The shared application context (it must be in the interactive menu).
        self_name: The advertised name of our node.
        self_key: The full public key of our node (hex).
        contacts: The known contacts to list under our node.
        prefix_bytes: The path hash width in bytes to highlight in each key.
        counts: The counts of overheard packets, keyed by the node id in lowercase (12 hex
            digits).
        sort: The initial sort (column and direction) over the ring of the shared list. It
            changes in place, so a new sort stays when the user visits a detail page and
            comes back.

    Raises:
        RuntimeError: If the caller is outside the interactive menu (no full-screen
            session).
    """
    from .node_detail_screen import open_node_detail
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the caller is for the menu only
        raise RuntimeError("the interactive contacts list is only available in the menu")
    session = ctx.ui.session

    def build() -> ContactsScreen:
        """The screen over the current contacts, with the live archived count on its tail."""
        return ContactsScreen(
            self_name,
            self_key,
            contacts,
            prefix_bytes,
            counts,
            sort,
            archived=_archived_count(ctx, self_key),
            locked=_locked_keys(ctx, self_key),
        )

    screen = build()
    while True:
        rebuild = False
        async with session.stay(screen) as visit:
            while True:
                chosen = await visit.result()
                if chosen is CANCEL or chosen is None:  # Esc
                    return
                if chosen == _ARCHIVE:
                    # The archive ladder, its preview, and its confirm all float over the
                    # list, which is already the backdrop. A sweep that archived a contact
                    # invalidates the contacts cache. Thus MeshTerm reads the list again and
                    # builds it again. This is the only deliberate reset in the life of this
                    # screen, because the rows in which it held a place are gone from the
                    # device.
                    if await archive_contacts(ctx, self_key):
                        rebuild = True
                        break
                    continue
                if chosen == _ARCHIVED:
                    # The archived list is a screen and not a section. It nests above this
                    # screen, and Esc comes back to this row. It makes this screen again
                    # only if the user restored or deleted a contact in it, because then
                    # this list is stale.
                    if await open_archived(ctx, self_key, prefix_bytes):
                        rebuild = True
                        break
                    continue
                # The sentinel of our node opens the page of our node. Each other value is a
                # Contact. The page nests above this list and has the verbs that manage one
                # contact: archive, restore, and delete. When one of them runs, the page
                # says so when it closes, and this list is built again without the row. This
                # is the same new read that the sweep does, for one contact at a time.
                if await open_node_detail(ctx, None if chosen == YOU else chosen):
                    rebuild = True
                    break
                # The page can lock or unlock the contact without an end to the visit. The
                # store answers from memory, so MeshTerm only draws the padlocks again.
                screen.refresh_locks(_locked_keys(ctx, self_key))
        if not rebuild:
            return
        contacts = await ctx.devstate.contacts()
        screen = build()


def _archived_count(ctx: AppContext, self_key: str) -> int:
    """The number of contacts archived off this device: the count and the gate of the tail row."""
    store = getattr(ctx, "contact_store", None)
    dev_pub = (self_key or "").lower().removeprefix("0x")
    if store is None or not dev_pub:
        return 0
    return len(store.archived(dev_pub))


def _locked_keys(ctx: AppContext, self_key: str) -> frozenset[str]:
    """The keys of the contacts locked on this device. These rows draw a padlock."""
    store = getattr(ctx, "contact_store", None)
    dev_pub = (self_key or "").lower().removeprefix("0x")
    if store is None or not dev_pub:
        return frozenset()
    return store.locked_keys(dev_pub)
