# SPDX-License-Identifier: Apache-2.0
"""The ``contacts`` tool: list the contacts that this node knows.

The menu opens the Contacts screen, which you can sort and which starts with our node.
The CLI prints only the contact list, because our node is not a contact.
``meshterm info`` reports our node, with much more detail than a row can hold.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.models import NODE_TYPE_LABELS
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.models import Contact
    from ..core.places import Place
    from ..ui.report import Listing


@register
class ContactsTool(Tool):
    """List this node and its known contacts, each with its hash."""

    name = "contacts"
    title = "Contacts"
    icon = "👥"
    help = "Known contacts — last heard, packets, type"
    category = "Message"
    order = 40  # the address book from which the three tools above select a recipient

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Query the device and render the contact list.

        Args:
            ctx: The shared application context.
            params: Not used.

        Returns:
            A :class:`ToolResult` with a summary of the number of known contacts.
        """
        from ..ui.surface import TuiUi
        from ..ui.widgets import ContactsSort, ordered_contacts

        # Read through the session cache. On a busy node, the contacts table is a slow
        # round trip. If MeshTerm reads it again (and the self info) at each visit to the
        # menu, that is a main cause of slow navigation. The cache keeps them for the session
        # and reads the contacts again in the background (refer to
        # :class:`~meshterm.services.device_state.DeviceState`).
        info = await ctx.devstate.self_info()
        contacts = await ctx.devstate.contacts()

        # The hash (the first part of a key, which a forced path uses as the address) is
        # ``mode + 1`` bytes wide. Light it in each key. The mode is an optional read. Thus
        # if the firmware does not report it, light nothing.
        try:
            mode = await ctx.devstate.path_hash_mode()
        except Exception:  # noqa: BLE001 - an optional read. If it is absent, nothing is lit.
            mode = None
        prefix_bytes = (mode + 1) if isinstance(mode, int) and 0 <= mode <= 3 else 0

        # The counts of overheard packets from the background monitor, keyed by node hash.
        # A contact that we never overheard passively has no entry.
        counts = {n.node: n.count for n in ctx.repo.heard_nodes() if n.node}

        self_name = str(info.get("name") or "this node")
        self_key = str(info.get("public_key") or "")
        # The list opens with the most recently heard node first. The list must answer "who
        # is out there now", and an order with the newest first answers that immediately.
        # A list from A to Z does not. Then the Ctrl+arrows sort the list again (and
        # ``--sort`` sets the order of the CLI).
        sort_name = str(params.get("sort") or "heard")

        # In the menu, give the list to the interactive screen, so that the Ctrl+arrows sort
        # it again while it is open. Their ring goes through the five columns of the Contacts
        # list (also the place and the hash). On the scripted CLI, print the plain listing one
        # time, in the requested order.
        if isinstance(ctx.ui, TuiUi):
            from ..ui.contactlist import CONTACTS_SORT_COLUMNS, CONTACTS_SORT_OPENS_ASCENDING
            from ..ui.contacts_screen import open_contacts

            sort = ContactsSort.from_name(
                sort_name, CONTACTS_SORT_COLUMNS, CONTACTS_SORT_OPENS_ASCENDING
            )
            await open_contacts(ctx, self_name, self_key, contacts, prefix_bytes, counts, sort)
            return ToolResult(summary={"contacts": len(contacts)})

        sort = ContactsSort.from_name(sort_name)
        # The CLI reads the stored places only. It never starts the job that finds them.
        places = getattr(ctx, "places", None)
        listing = _listing(
            ordered_contacts(contacts, counts, sort),
            counts,
            prefix_bytes,
            places.get if places is not None else None,
        )
        return ToolResult(
            summary={"contacts": len(contacts)},
            report=(listing,),
            exit_code=exitcodes.OK if contacts else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``contacts`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command
        from ..ui.widgets import ContactsSort

        @app.command(name=self.name, help=self.help)
        def _contacts(
            sort: str = typer.Option(
                "heard", "--sort", "-s", help="Order contacts by: heard, name, packets"
            ),
        ) -> None:
            # For an unknown name, `ContactsSort.from_name` uses the first column. This is
            # on purpose, so that a saved preference from an older build cannot block the
            # list of the menu. On the command line, the same silence hides a typing error:
            # the caller asked for one order and got another, and nothing said so.
            if sort not in ContactsSort.names():
                choices = ", ".join(ContactsSort.names())
                raise typer.BadParameter(f"--sort must be one of: {choices} (got {sort!r})")
            run_tool_command(self, {"sort": sort})


def _listing(
    contacts: list[Contact],
    counts: dict[str, int],
    prefix_bytes: int,
    place_of: Callable[[str], Place | None] | None = None,
) -> Listing:
    """The contact list, stated one time for the two faces.

    ``TYPE`` is the advertised role of the node, in words. The menu draws a coloured glyph
    there, but a monochrome ``▲`` must have a legend, and the CLI has no legends.
    ``HEARD`` is a relative age, because the user opens this listing to ask "recently?".
    ``PLACE`` is the named area nearest the location of the node. It is just before ``KEY``.
    ``HASH`` is the token that ``--path`` and ``--to`` accept. Before, the user had to cut
    it out of ``KEY`` by hand. ``LOCATION`` is a fact that the model always had, but no
    column had space for it. ``KEY`` stays full and last. Thus it goes off the right side
    with no harm and is never cut: a caller cannot give a truncated key back to a command.

    Args:
        contacts: The contacts, already in the requested order.
        counts: The counts of overheard packets, keyed by node id (from the passive
            monitor).
        prefix_bytes: The path hash width of the device. Because of it, ``HASH`` is the
            token by which a forced path addresses this node, not a random part of the
            key.
        place_of: Gives the stored place of a key, or ``None``. ``PLACE`` is the label of
            that place, and it is ``null`` in the document when there is none.

    Returns:
        The listing.
    """
    from ..ui import fields
    from ..ui.report import Listing
    from ..ui.widgets import contact_packets

    rows = []
    for contact in contacts:
        key = (contact.public_key or "").lower()
        place = place_of(key or contact.key_prefix) if place_of else None
        rows.append(
            {
                "node": fields.NodeRef(
                    name=contact.name,
                    key=key or None,
                    hash=(key or contact.key_prefix.lower())[: prefix_bytes * 2] or None,
                    type=NODE_TYPE_LABELS.get(contact.node_type),
                    place=(place.label or None) if place else None,
                ),
                "heard_at": contact.last_seen,
                "packets": contact_packets(contact, counts),
                "position": (
                    fields.Position(contact.lat, contact.lon) if contact.has_location else None
                ),
            }
        )
    return Listing(
        key="contacts",
        columns=(
            fields.node(
                "node",
                lanes=(
                    ("name", "NAME"),
                    ("type", "TYPE"),
                    ("hash", "HASH"),
                    ("place", "PLACE"),
                    ("key", "KEY"),
                ),
            ),
            fields.when("heard_at", "HEARD", absent="never"),
            fields.integer("packets", "PKTS"),
            fields.position(),
        ),
        rows=rows,
        # The five lanes of the node are not together. What the user looks for (who, what,
        # how recently, how much) comes first, and the long fields go to the right. PLACE is
        # just before KEY, the same as in the menu.
        order=("NAME", "TYPE", "HEARD", "PKTS", "HASH", "LOCATION", "PLACE", "KEY"),
    )
