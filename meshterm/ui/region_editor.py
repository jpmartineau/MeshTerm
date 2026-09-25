# SPDX-License-Identifier: Apache-2.0
"""The region editor: which floods a repeater relays, region by region, over the mesh.

Reached from a repeater-admin session's *Regions* row (see :mod:`meshterm.ui.repeater_admin`)
once logged in. A repeater keeps a small table of regions in a tree under the wildcard
``*`` and relays a scoped flood only when that flood's region is one it allows; the
wildcard's own flag says whether it relays plain, unscoped floods at all. This page reads
the table over the remote CLI (``region``, parsed by
:func:`~meshterm.core.region_admin.parse_region_dump`), draws it as a tree — region, whether
floods are allowed, and a note for the unscoped wildcard, the home region and the default
scope — and edits it one command at a time.

**Edits apply at once; saving is the commit.** The settings editor next door stages values
and sends them on Apply, because a staged value costs nothing and a sent one is final. A
region edit is the other way round: the repeater itself is the staging area. Every command
changes the table in its RAM and nothing reaches flash until ``region save``, so a reboot
undoes whatever was not saved. Staging a second time on this side would only add a second
list of pending changes that could disagree with the first — and region edits depend on
each other (a region must exist before it can be allowed, emptied before it is removed),
so a staged batch would have to replay the firmware's rules to know it would work. So each
action sends its one command and the tree redraws from the reply
(:meth:`~meshterm.core.region_admin.RegionTable.after`), the title counts the edits not yet
saved, and the page ends on the same Apply/discard pair the editors use
(:func:`~meshterm.ui.menus.exit_rows`) — worded for what it is here: *Save n changes to the
repeater* over *Back — a reboot undoes them*. Leaving with edits unsaved asks first.
``region default`` saves the whole table itself on firmware that has it
(:func:`~meshterm.core.region_admin.saves_table`), so the count drops to zero there too.

**A dump the reply cap cut is said to be cut.** Every CLI reply fits in 160 bytes and a
real tree does not, so a dump near the cap is read as possibly cut, its half-line dropped,
and the flat ``region list allowed``/``denied`` read to recover the rest — shown under
*Beyond the cut*, their place in the tree unknown, and still editable by name. A list near
its own cap may have skipped a name, and the page says so rather than claiming a complete
table.

Whatever the table says the repeater relays is learned into the region store
(:meth:`~meshterm.core.region_store.RegionStore.learn_carried`), so the node page and scope
resolution know it without asking again — replacing its previous answer only when the table
is known whole, and only adding to it when it may not be.

Every command is one paced transmission the reader asked for: the read is two to four
commands under an abortable progress dialog with the trace cooldown between them, and each
edit is one command under the busy overlay. Nothing polls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.cells import cell_len
from rich.text import Text

from ..core.models import Contact
from ..core.region_admin import (
    DEFAULT_QUERY,
    DUMP_COMMAND,
    LIST_ALLOWED,
    LIST_DENIED,
    SAVE_COMMAND,
    RegionRow,
    RegionTable,
    allow_command,
    default_command,
    deny_command,
    home_command,
    parse_region_dump,
    put_command,
    region_refused,
    remove_command,
    saves_table,
    validate_new_name,
)
from ..core.regions import WILDCARD, RegionNameError
from .menus import (
    Lane,
    column_header,
    icon_lane,
    lane_row,
    marked_label,
    menu_rows,
    run_steps,
    section_heading,
)
from .tui import Choice, Separator
from .tui.select import DeleteRequest, SelectScreen

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device

#: Seconds to wait for one reply — the admin page's own patience (multi-hop takes seconds).
_REPLY_TIMEOUT_S = 10.0

#: The page's key hint. ``Del remove`` is spliced in on the rows it would act on.
REGION_HINT = "↑↓ move · type to filter · Enter open · ^R read · Esc back"
REMOVE_HINT = "Del remove"

# Action sentinels. Region rows carry a :class:`RegionPick` instead, so no region name —
# which may hold underscores — can ever be mistaken for one of these.
_ADD = "__add__"
_READ = "__read__"
_SAVE = "__save__"
_BACK = "__back__"


@dataclass(frozen=True, slots=True)
class RegionPick:
    """A region row's value: the region it names.

    Attributes:
        name: The region's name, ``*`` for the wildcard row.
    """

    name: str


class RegionMenu(SelectScreen):
    """The region editor's list, plus ``^R`` to read the table again.

    A full-screen page, not a popup: the reader works in it for a while, it is the tool's
    own view of one repeater, and every question it asks (a region's actions, a new name, a
    confirm) floats over it. ``^R`` is the admin page's *read again* chord for the same
    reason it is there — the mesh is asked the same question again — and here it re-reads
    the whole table, which is the only read there is.

    On the F-key lane ``Read`` takes F3, with ``Remove`` as its Shift half, lit only on a
    region the firmware would let go (no sub-regions, never the wildcard) — the same pair of
    this-row verbs the admin page puts on that slot.
    """

    floating = False

    def __init__(self, title: str, items: list, **kwargs: Any) -> None:
        """Build the page; rows end at the edge, as every editor lane does."""
        kwargs.setdefault("hscroll", False)
        kwargs.setdefault("footer_hint", REGION_HINT)
        kwargs.setdefault("delete_hint", REMOVE_HINT)
        super().__init__(title, items, **kwargs)

    def _removable(self) -> bool:
        """Whether the highlighted row is a region Del would offer to remove."""
        current = self._current_choice()
        return current is not None and current.deletable

    @property
    def fkey_lane(self):
        """The list's lane with ``Read`` on F3 and ``Remove`` behind it."""
        from .tui.fkeys import FPair

        lane = list(super().fkey_lane)
        lane[2] = FPair(
            "Read",
            "retry",
            opp_label="Remove",
            opp_action="delete",
            opp_enabled=self._removable(),
        )
        return lane

    def handle(self, action: str, data: str = "") -> None:
        """Read the table again on ``^R``, or behave as any select list does."""
        if action == "retry":
            self.resolve(_READ)
            return
        super().handle(action, data)


# --- the page ------------------------------------------------------------------------


def node_id_of(node: Contact) -> str:
    """The 12-hex id the region store files a repeater under."""
    return (node.public_key or node.key_prefix or "").lower().removeprefix("0x")[:12]


def region_items(node: Contact, table: RegionTable, unsaved: int) -> tuple[str, list]:
    """The editor's title and rows for one table and its count of unsaved edits.

    The tree first — the wildcard, then every region indented one step per level — in the
    same REGION / FLOOD / NOTE lanes the other editors draw their settings in, headed by a
    pinned column header. Then, when the dump was cut, a note saying where, and the regions
    recovered from the flat lists under their own heading. Then the page's actions, and
    last the save/leave pair while anything is unsaved.
    """
    placed = [row for row in table.rows if row.placed]
    unplaced = [row for row in table.rows if not row.placed]
    lines = [(_row_label(row), _flood_text(row), _row_note(row, table), row) for row in table.rows]
    label_w = max([cell_len("REGION")] + [cell_len(label) for label, _, _, _ in lines])
    value_w = max([cell_len("FLOOD")] + [cell_len(value.plain) for _, value, _, _ in lines])

    items: list = [
        Separator(
            lambda w: column_header(
                [Lane("REGION", label_w + 2), Lane("FLOOD", value_w + 2), Lane("NOTE")], w
            ),
            pinned=True,
        ),
        section_heading("Regions"),
    ]
    for label, value, note, row in lines:
        if row.placed:
            items.append(_region_choice(label, value, note, row, table, label_w, value_w))
    if not [row for row in placed if not row.wildcard] and not unplaced:
        items.append(Separator(Text("no regions — only the wildcard", style="muted")))
    if table.cut:
        where = f" after {table.cut_after}" if table.cut_after else ""
        items.append(Separator(Text(f"⚠ the reply ran out at 160 bytes{where}", style="warn")))
        if not table.lists_read:
            items.append(
                Separator(Text("  regions past it are not shown — ^R reads again", style="warn"))
            )
    if unplaced:
        items.append(section_heading("Beyond the cut"))
        items.append(Separator(Text("from the flat lists — place in the tree unknown", "muted")))
        for label, value, note, row in lines:
            if not row.placed:
                items.append(_region_choice(label, value, note, row, table, label_w, value_w))
    if table.cut and table.lists_read and table.lists_partial:
        items.append(Separator(Text("⚠ the lists were long too — some may be missing", "warn")))

    actions = [
        ("＋", "Add region…", "A new region, floods allowed", _ADD),
        ("↻", "Read regions", "Ask the repeater for its table again", _READ),
    ]
    lane = icon_lane(icon for icon, _, _, _ in actions)
    # A blank line between the table and the page's commands: two lists, not one. The tree's
    # first row is the wildcard, whose name ``*`` reads as a leading mark to anything that
    # measures icon columns — it is data, and it answers to no column the actions keep.
    items.append(Separator(" "))
    items.append(section_heading("Actions"))
    items.extend(
        menu_rows(
            (marked_label(icon, label, "", lane=lane), help_text, value)
            for icon, label, help_text, value in actions
        )
    )
    items.extend(_save_rows(unsaved))

    title = f"Regions — {node.name}" + (f" · {unsaved} unsaved" if unsaved else "")
    return title, items


def _region_choice(
    label: str,
    value: Text,
    note: str,
    row: RegionRow,
    table: RegionTable,
    label_w: int,
    value_w: int,
) -> Choice:
    """One region row; Del removes it only where the firmware would let it go."""
    return Choice(
        title=lane_row(label, value, note, label_w, value_w),
        value=RegionPick(row.name),
        deletable=not row.wildcard and not table.children(row.name),
    )


def _row_label(row: RegionRow) -> str:
    """The REGION lane: the name, indented two cells per level below the top."""
    return "  " * max(0, row.depth - 1) + row.name if row.placed else row.name


def _flood_text(row: RegionRow) -> Text:
    """The FLOOD lane: the firmware's own two words (``allowf``/``denyf``)."""
    return Text("allowed", style="ok") if row.flood else Text("denied", style="muted")


def _row_note(row: RegionRow, table: RegionTable) -> str:
    """The NOTE lane: what the row *is* beyond its flag — unscoped, home, default."""
    if row.wildcard:
        return "unscoped floods"
    notes = []
    if row.home:
        notes.append("home")
    if table.default == row.name:
        notes.append("default scope")
    return " · ".join(notes)


def _save_rows(unsaved: int) -> list:
    """The editors' Apply/discard pair, worded for a table that is live but not saved.

    Same shape as :func:`~meshterm.ui.menus.exit_rows` — nothing while clean, then a blank
    line and the pair — with its words changed, because here nothing is discarded by
    leaving: the edits are already live on the repeater, and it is the next reboot that
    undoes them.
    """
    if not unsaved:
        return []
    noun = "change" if unsaved == 1 else "changes"
    return [
        Separator(" "),
        Choice(
            title=Text.assemble(("✓ ", "ok"), f"Save {unsaved} {noun} to the repeater"),
            value=_SAVE,
        ),
        Choice(title=Text.assemble(("✗ ", "err"), "Back — a reboot undoes them"), value=_BACK),
    ]


# --- the visit ----------------------------------------------------------------------


async def open_region_editor(ctx: AppContext, device: Device, node: Contact) -> None:
    """Read ``node``'s region table and run the editor over it until the reader leaves.

    The node must already be logged in (the admin session's caller did that). A table that
    cannot be read at all — no reply, or firmware without the ``region`` command — is said
    in a popup and the page never opens: there would be nothing on it to edit.

    Args:
        ctx: The shared application context (interactive TUI).
        device: The connected companion.
        node: The repeater being administered.
    """
    from .tui import CANCEL

    session = ctx.ui.session
    table = await read_table(ctx, device, node)
    if table is None:
        return
    unsaved = 0
    title, items = region_items(node, table, unsaved)
    menu = RegionMenu(title, items)
    async with session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            if choice is CANCEL or choice is None or choice == _BACK:
                if unsaved and not await _confirm_leave(ctx, node, unsaved):
                    continue
                return
            if choice == _READ:
                fresh = await read_table(ctx, device, node)
                if fresh is not None:
                    table = fresh
            elif choice == _SAVE:
                table, unsaved = await _edit(ctx, device, node, table, unsaved, SAVE_COMMAND)
            elif choice == _ADD:
                table, unsaved = await _add_region(ctx, device, node, table, unsaved, None)
            elif isinstance(choice, DeleteRequest) and isinstance(choice.value, RegionPick):
                table, unsaved = await _remove(ctx, device, node, table, unsaved, choice.value.name)
            elif isinstance(choice, RegionPick):
                table, unsaved = await _region_actions(
                    ctx, device, node, table, unsaved, choice.name
                )
            title, items = region_items(node, table, unsaved)
            menu.replace_items(items, title=title)


async def read_table(ctx: AppContext, device: Device, node: Contact) -> RegionTable | None:
    """Read the whole table, paced, under an abortable progress dialog.

    ``region`` for the tree, then ``region default`` for the default scope (firmware 1.15+;
    older firmware's refusal just leaves it unknown), then — only when the tree came back
    cut — the two flat lists that recover what the cut hid. What was read is learned into
    the region store.

    Returns:
        The table, or ``None`` when the dump never came (no reply, the firmware has no
        ``region`` command, or the reader aborted) — after saying which.
    """
    from .repeater_admin import _run_under_dialog

    session = ctx.ui.session
    replies: dict[str, str | None] = {}

    async def work(dialog: Any) -> None:
        plan = [DUMP_COMMAND, DEFAULT_QUERY]
        i = 0
        while i < len(plan):
            command = plan[i]
            if i:
                await asyncio.sleep(ctx.preferences.trace_cooldown_s)
            dialog.status = command
            session.invalidate()
            reply = await device.send_remote_command(node, command, timeout=_REPLY_TIMEOUT_S)
            replies[command] = reply
            if command == DUMP_COMMAND:
                if reply is None or region_refused(reply):
                    return  # nothing else is worth asking without the tree
                if parse_region_dump(reply).cut:
                    plan += [LIST_ALLOWED, LIST_DENIED]
            i += 1

    aborted = await _run_under_dialog(ctx, f"Reading regions — {node.name}", work)
    dump = replies.get(DUMP_COMMAND)
    if aborted:
        return None
    if dump is None:
        await session.message_dialog(
            Text(
                f"No reply from {node.name} to region — it may be out of reach or busy; "
                "try again when it answers.",
                style="warn",
            ),
            title="Regions",
        )
        return None
    if region_refused(dump):
        await session.message_dialog(
            Text(
                f"{node.name} does not know the region command ({dump.strip()}). "
                "Regions need repeater firmware 1.12 or newer.",
                style="err",
            ),
            title="Regions",
        )
        return None
    table = parse_region_dump(dump).with_default(replies.get(DEFAULT_QUERY))
    if table.cut:
        table = table.with_lists(replies.get(LIST_ALLOWED), replies.get(LIST_DENIED))
    learn_table(ctx, node, table)
    return table


def learn_table(ctx: AppContext, node: Contact, table: RegionTable) -> None:
    """Tell the region store what this repeater relays, as far as the table can say.

    A table known whole replaces the repeater's previous answer, exactly as the anonymous
    regions request would; one that may be missing names only adds what it shows, so a cut
    never makes the store forget a region the repeater still carries.
    """
    store = getattr(ctx, "region_store", None)
    node_id = node_id_of(node)
    if store is None or not node_id:
        return
    if table.complete and table.wildcard is not None:
        store.learn_carried(node_id, table.carried())
        return
    for name in table.carried():
        if name != WILDCARD:
            store.learn(name, "repeater", repeater=node_id)


async def _edit(
    ctx: AppContext,
    device: Device,
    node: Contact,
    table: RegionTable,
    unsaved: int,
    command: str,
) -> tuple[RegionTable, int]:
    """Send one region command and fold an accepted reply into the table.

    A refused command shows the repeater's own words; a silent one says the change may
    still have landed and points at ``^R``, since only a read can tell. An accepted edit
    counts as unsaved unless it saved the table itself (``region save``, and ``region
    default`` on firmware that auto-saves it).

    Returns:
        The table and the unsaved count, after the command.
    """
    async with ctx.ui.busy_overlay():
        reply = await device.send_remote_command(node, command, timeout=_REPLY_TIMEOUT_S)
    if reply is None:
        await ctx.ui.session.message_dialog(
            Text(
                f"No reply to {command} — it may still have landed. ^R reads the table again.",
                style="warn",
            ),
            title=f"Regions — {node.name}",
        )
        return table, unsaved
    if region_refused(reply):
        await ctx.ui.session.message_dialog(
            Text(f"{node.name} refused {command}: {reply.strip()}", style="err"),
            title=f"Regions — {node.name}",
        )
        return table, unsaved
    table = table.after(command, reply)
    unsaved = 0 if saves_table(command, reply) else unsaved + 1
    learn_table(ctx, node, table)
    return table, unsaved


async def _region_actions(
    ctx: AppContext,
    device: Device,
    node: Contact,
    table: RegionTable,
    unsaved: int,
    name: str,
) -> tuple[RegionTable, int]:
    """What can be done to one region — a question asked in a popup, then done.

    Every row is one command except *Add region under it…*, which asks a name, and
    *Remove…*, which asks first. Denying the wildcard asks too: it stops every plain flood
    at this repeater, which is most of the mesh's traffic.
    """
    row = table.get(name)
    if row is None:
        return table, unsaved
    rows: list[tuple[str | Text, str, str]] = []
    subject = "unscoped floods" if row.wildcard else f"floods scoped to {name}"
    if row.flood:
        rows.append(("Deny flood", f"Stop relaying {subject}", "deny"))
    else:
        rows.append(("Allow flood", f"Relay {subject}", "allow"))
    if row.wildcard:
        if table.home not in (None, WILDCARD):
            rows.append(("Clear home", f"No home region (now {table.home})", "home"))
    elif not row.home:
        rows.append(("Make home", "Mark it as this repeater's own region", "home"))
    if not row.wildcard and table.default_known:
        if table.default == name:
            rows.append(("Clear default", "Its own floods go unscoped; saves all", "undefault"))
        else:
            rows.append(("Make default", "Scope its own floods to it; saves all", "default"))
    under = "top level" if row.wildcard else f"inside {name}"
    rows.append(("Add region…" if row.wildcard else "Add region under it…", f"New, {under}", "add"))
    if not row.wildcard:
        children = table.children(name)
        if children:
            rows.append(("Remove…", "Remove its sub-regions first", "blocked"))
        else:
            rows.append((Text("Remove…", style="err"), "Delete it from the table", "remove"))

    picked = await ctx.ui.select(
        f"Region — {name}",
        menu_rows(rows),
        prompt=_state_line(row, table),
        filterable=False,
        floating=True,
    )
    if picked is None:
        return table, unsaved
    if picked == "deny":
        if row.wildcard and not await _confirm_deny_unscoped(ctx, node):
            return table, unsaved
        return await _edit(ctx, device, node, table, unsaved, deny_command(name))
    if picked == "allow":
        return await _edit(ctx, device, node, table, unsaved, allow_command(name))
    if picked == "home":
        return await _edit(ctx, device, node, table, unsaved, home_command(name))
    if picked == "default":
        return await _edit(ctx, device, node, table, unsaved, default_command(name))
    if picked == "undefault":
        return await _edit(ctx, device, node, table, unsaved, default_command(None))
    if picked == "add":
        return await _add_region(ctx, device, node, table, unsaved, name)
    if picked == "remove":
        return await _remove(ctx, device, node, table, unsaved, name)
    if picked == "blocked":
        names = ", ".join(child.name for child in table.children(name))
        await ctx.ui.session.message_dialog(
            Text(f"{name} still holds {names}. Remove those first.", style="warn"),
            title=f"Region — {name}",
        )
    return table, unsaved


def _state_line(row: RegionRow, table: RegionTable) -> str:
    """The popup's one-line summary of a region: its flag, its place, its notes."""
    atoms = ["floods allowed" if row.flood else "floods denied"]
    if row.wildcard:
        atoms.append("the unscoped case")
    elif not row.placed:
        atoms.append("place unknown")
    elif row.parent and row.parent != WILDCARD:
        atoms.append(f"inside {row.parent}")
    note = _row_note(row, table)
    if note and not row.wildcard:
        atoms.append(note)
    return " · ".join(atoms)


async def _add_region(
    ctx: AppContext,
    device: Device,
    node: Contact,
    table: RegionTable,
    unsaved: int,
    parent: str | None,
) -> tuple[RegionTable, int]:
    """Add a region: where (unless the row it was asked from already said), then its name.

    Two steps through :func:`~meshterm.ui.menus.run_steps`, so Esc on the name goes back to
    the parent picker with its answer highlighted. The name is checked against the
    firmware's own rules and against the table: ``region put`` of a name already there
    *moves* that region, which is never what *Add* means.
    """
    taken = [row.name for row in table.rows]

    async def pick_parent(values: list) -> str | None:
        candidates = [row for row in table.rows if row.placed]
        items = [
            Choice(
                title=("  " * max(0, row.depth - 1) + row.name)
                + ("  (top level)" if row.wildcard else ""),
                value=row.name,
            )
            for row in candidates
        ]
        return await ctx.ui.select(
            "Add region — where",
            items,
            prompt="The region to put it inside (* for the top level):",
            default=values[0] or WILDCARD,
            floating=True,
        )

    # The name is the last step either way; where it sits in the answers is the only
    # difference between being asked from a row (one step) and from Actions (two).
    slot = 0 if parent is not None else 1

    async def ask_name(values: list) -> str | None:
        where = parent if parent is not None else values[0]
        inside = "at the top level" if where == WILDCARD else f"inside {where}"

        def check(text: str) -> bool | str:
            try:
                validate_new_name(text, taken)
            except RegionNameError as exc:
                return str(exc)
            return True

        typed = await ctx.ui.text(
            "Add region",
            prompt=f"Name of the new region, {inside}:",
            default=values[slot] or "",
            validate=check,
            help_text="letters, digits and -, up to 30 bytes",
            floating=True,
        )
        return typed.strip() if typed else None

    answers = await run_steps([ask_name] if parent is not None else [pick_parent, ask_name])
    if answers is None:
        return table, unsaved
    where = parent if parent is not None else answers[0]
    name = answers[slot]
    try:
        bare = validate_new_name(name, taken)
    except RegionNameError:  # pragma: no cover - the prompt's validator already refused it
        return table, unsaved
    return await _edit(ctx, device, node, table, unsaved, put_command(bare, where))


async def _remove(
    ctx: AppContext,
    device: Device,
    node: Contact,
    table: RegionTable,
    unsaved: int,
    name: str,
) -> tuple[RegionTable, int]:
    """Remove one region behind the red single-record confirm."""
    from .tui import CANCEL

    row = table.get(name)
    if row is None or row.wildcard or table.children(name):
        return table, unsaved
    choice = await ctx.ui.dialog(
        f"Remove {name} from {node.name}? Floods scoped to it stop being relayed here "
        "at once; region save makes it permanent.",
        [("Cancel", CANCEL), ("Remove", "remove")],
        title="Remove region",
        default=1,
        destructive=True,
    )
    if choice != "remove":
        return table, unsaved
    return await _edit(ctx, device, node, table, unsaved, remove_command(name))


async def _confirm_deny_unscoped(ctx: AppContext, node: Contact) -> bool:
    """Ask before a repeater stops relaying plain floods — most of the mesh's traffic."""
    choice = await ctx.ui.dialog(
        f"Stop {node.name} relaying unscoped floods? Every plain flood — adverts and most "
        "messages today — would stop here, and only scoped floods would pass.",
        [("Cancel", None), ("Deny", "deny")],
        title="Deny unscoped floods",
        default=1,
        danger=True,
    )
    return choice == "deny"


async def _confirm_leave(ctx: AppContext, node: Contact, unsaved: int) -> bool:
    """Ask before leaving edits live but unsaved; ``True`` means leave anyway."""
    noun = "change is" if unsaved == 1 else "changes are"
    choice = await ctx.ui.dialog(
        f"{unsaved} {noun} live on {node.name} but not saved — its next reboot undoes "
        f"{'it' if unsaved == 1 else 'them'}. Leave without saving?",
        [("Keep editing", "keep"), ("Leave", "leave")],
        title="Unsaved regions",
        default=1,
        danger=True,
    )
    return choice == "leave"
