# SPDX-License-Identifier: Apache-2.0
"""The region editor: the floods that a repeater relays, region by region, over the mesh.

The user reaches it from the *Regions* row of a repeater-admin session, after the log in
(refer to :mod:`meshterm.ui.repeater_admin`). A repeater keeps a small table of regions in
a tree under the wildcard ``*``. It relays a scoped flood only when the region of that flood
is a region that the repeater allows. The flag of the wildcard says whether the repeater
relays plain, unscoped floods at all. This page reads the table over the remote CLI
(``region``, parsed by :func:`~meshterm.core.region_admin.parse_region_dump`). It draws the
table as a tree with these items: the region, whether floods are allowed, and a note for the
unscoped wildcard, the home region, and the default scope. The user edits the table one
command at a time.

**Edits apply at once, and the save is the commit.** The settings editor next to this one
stages values and sends them at Apply. A staged value costs nothing, and a value that
MeshTerm sent is final. A region edit is the opposite. The repeater itself is the staging
area. Each command changes the table in the RAM of the repeater, and nothing goes to flash
until ``region save``. Thus a reboot undoes each edit that was not saved. A second staging
on this side would only add a second list of pending changes, and the two lists could
disagree. Also, region edits depend on each other. A region must exist before the user can
allow it, and it must be empty before the user can remove it. Thus a staged batch would
have to repeat the rules of the firmware to know that it works. For these reasons each
action sends its one command, and the tree is drawn again from the reply
(:meth:`~meshterm.core.region_admin.RegionTable.after`). The title counts the edits that are
not yet saved. The page ends with the same Apply/discard pair that the editors use
(:func:`~meshterm.ui.menus.exit_rows`), with words that fit this case: *Save n changes to
the repeater* over *Back — a reboot undoes them*. If the user leaves with unsaved edits, the
page asks first. On firmware that has it, ``region default`` saves the whole table itself
(:func:`~meshterm.core.region_admin.saves_table`), so the count also goes to zero there.

**The page says when the reply cap cut a dump.** Each CLI reply fits in 160 bytes, and a real
tree does not fit. Thus MeshTerm reads a dump that is near the cap as possibly cut. It
removes the half line at the end. Then it reads the flat lists ``region list allowed`` and
``region list denied`` to recover the rest. The page shows these regions under *Beyond the
cut*. Their place in the tree is not known, and the user can still edit them by name. A
list that is near its own cap may have skipped a name. The page says this, and it does not
claim a complete table.

MeshTerm learns what the table says that the repeater relays into the region store
(:meth:`~meshterm.core.region_store.RegionStore.learn_carried`). Thus the node page and the
scope resolution know it without a new request. The new table replaces the previous answer
only when the table is known to be whole. When the table can be incomplete, the new table
only adds to the previous answer.

Each command is one paced transmission that the user asked for. The read is two to four
commands, under a progress dialog that the user can abort, with the trace cooldown between
the commands. Each edit is one command under the busy overlay. Nothing polls.
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

#: The seconds to wait for one reply. This is the same wait as on the admin page (a reply
#: over many hops takes seconds).
_REPLY_TIMEOUT_S = 10.0

#: The key hint of the page. The code adds ``Del remove`` on the rows where it acts.
REGION_HINT = "↑↓ move · type to filter · Enter open · ^R read · Esc back"
REMOVE_HINT = "Del remove"

# The action sentinels. Region rows have a :class:`RegionPick` instead. Thus the code can
# never take a region name (which can have underscores) for one of these.
_ADD = "__add__"
_READ = "__read__"
_SAVE = "__save__"
_BACK = "__back__"


@dataclass(frozen=True, slots=True)
class RegionPick:
    """The value of a region row: the region that the row names.

    Attributes:
        name: The name of the region. It is ``*`` for the wildcard row.
    """

    name: str


class RegionMenu(SelectScreen):
    """The list of the region editor, with ``^R`` to read the table again.

    This is a full-screen page and not a dialog. The user works in it for some time, and it
    is the own view of the tool for one repeater. Each question that it asks (the actions of
    a region, a new name, a confirm) floats over it. ``^R`` is the *read again* chord of
    the admin page, and it is here for the same reason: the mesh gets the same question
    again. Here it reads the whole table again, which is the only read that exists.

    On the F-key lane, ``Read`` is on F3, and ``Remove`` is its Shift half. ``Remove`` is lit
    only on a region that the firmware lets go (no sub-regions, and never the wildcard). The
    admin page puts the same pair of verbs for the row on that slot.
    """

    floating = False

    def __init__(self, title: str, items: list, **kwargs: Any) -> None:
        """Build the page. The rows end at the edge, as each editor lane does."""
        kwargs.setdefault("hscroll", False)
        kwargs.setdefault("footer_hint", REGION_HINT)
        kwargs.setdefault("delete_hint", REMOVE_HINT)
        super().__init__(title, items, **kwargs)

    def _removable(self) -> bool:
        """Whether the highlighted row is a region that Del offers to remove."""
        current = self._current_choice()
        return current is not None and current.deletable

    @property
    def picocalc_lyra_lane(self):
        """The lane of the list, with ``Read`` on F3 and ``Remove`` behind it."""
        from .tui.fkeys import FPair

        lane = list(super().picocalc_lyra_lane)
        lane[2] = FPair(
            "Read",
            "retry",
            opp_label="Remove",
            opp_action="delete",
            opp_enabled=self._removable(),
        )
        return lane

    def handle(self, action: str, data: str = "") -> None:
        """Read the table again on ``^R``. Other keys act as in each select list."""
        if action == "retry":
            self.resolve(_READ)
            return
        super().handle(action, data)


# --- the page ------------------------------------------------------------------------


def node_id_of(node: Contact) -> str:
    """The 12-hex id under which the region store files a repeater."""
    return (node.public_key or node.key_prefix or "").lower().removeprefix("0x")[:12]


def region_items(node: Contact, table: RegionTable, unsaved: int) -> tuple[str, list]:
    """The title and the rows of the editor for one table and its count of unsaved edits.

    The order of the page is:

    1. The tree: the wildcard, then each region with one indent step for each level. The
       lanes are REGION, FLOOD, and NOTE, as the lanes in which the other editors draw their
       settings. A pinned column header is above them.
    2. If the dump was cut, a note that says where. Then the regions that the code recovered
       from the flat lists, under their own heading.
    3. The actions of the page.
    4. Last, the save and leave pair, while anything is unsaved.
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
    # A blank line between the table and the commands of the page, because they are two lists
    # and not one. The first row of the tree is the wildcard. Code that measures icon
    # columns reads its name ``*`` as a leading mark. But the name is data, and it does not
    # belong to a column that the actions keep.
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
    """One region row. Del removes it only where the firmware lets it go."""
    return Choice(
        title=lane_row(label, value, note, label_w, value_w),
        value=RegionPick(row.name),
        deletable=not row.wildcard and not table.children(row.name),
    )


def _row_label(row: RegionRow) -> str:
    """The REGION lane: the name, with an indent of two cells for each level below the top."""
    return "  " * max(0, row.depth - 1) + row.name if row.placed else row.name


def _flood_text(row: RegionRow) -> Text:
    """The FLOOD lane: the two words of the firmware itself (``allowf`` and ``denyf``)."""
    return Text("allowed", style="ok") if row.flood else Text("denied", style="muted")


def _row_note(row: RegionRow, table: RegionTable) -> str:
    """The NOTE lane: what the row is, besides its flag: unscoped, home, or default.

    The ``^`` of the wildcard is not a home. When no region is the home, the firmware puts the
    home mark on ``*``. Thus ``*^`` means *no home region set*, and the note says this
    (``unscoped · no home``, which is short enough to stay whole in the lane of 72 columns).
    The note does not leave the one meaning of the mark unsaid.
    """
    if row.wildcard:
        return "unscoped · no home" if row.home else "unscoped floods"
    notes = []
    if row.home:
        notes.append("home")
    if table.default == row.name:
        notes.append("default scope")
    return " · ".join(notes)


def _save_rows(unsaved: int) -> list:
    """The Apply/discard pair of the editors, with words for a table that is live but not saved.

    The shape is the same as :func:`~meshterm.ui.menus.exit_rows`: nothing while the page is
    clean, then a blank line and the pair. The words are different, because here the user
    does not discard anything when the user leaves. The edits are already live on the
    repeater, and the next reboot undoes them.
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
    """Read the region table of ``node`` and run the editor over it until the user leaves.

    The user must already be logged in to the node (the caller of the admin session did
    that). If MeshTerm cannot read the table at all (no reply, or firmware without the
    ``region`` command), a dialog says so, and the page never opens, because there is
    nothing on it to edit.

    Args:
        ctx: The shared application context (interactive TUI).
        device: The connected companion.
        node: The repeater that the user administers.
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
    """Read the whole table, paced, under a progress dialog that the user can abort.

    The function sends these commands in order:

    1. ``region`` for the tree.
    2. ``region default`` for the default scope (firmware 1.15 and later). Older firmware
       refuses it, and then the default scope stays unknown.
    3. The two flat lists that recover what the cut hid. The function sends them only when
       the tree came back cut.

    MeshTerm learns what the function read into the region store.

    Returns:
        The table. It returns ``None`` when the dump never came (no reply, the firmware has
        no ``region`` command, or the user aborted), after it says which case it was.
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
                    return  # without the tree, no other command has a use
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
    """Tell the region store what this repeater relays, as far as the table can show.

    A table that is known to be whole replaces the previous answer of the repeater, as the
    anonymous regions request does. A table that can miss names only adds what it shows.
    Thus a cut never makes the store forget a region that the repeater still carries.
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
    """Send one region command and put an accepted reply into the table.

    If the repeater refuses the command, the dialog shows the words of the repeater. If the
    repeater does not reply, the dialog says that the change may still have happened, and it
    refers to ``^R``, because only a read can tell. An accepted edit counts as unsaved,
    except when it saved the table itself (``region save``, and ``region default`` on
    firmware that saves it automatically).

    Returns:
        The table and the count of unsaved edits, after the command.
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
    """The actions for one region: a question in a dialog, then the action.

    Each row is one command, except *Add region under it…*, which asks for a name, and
    *Remove…*, which asks for a confirm first. The code also asks before it denies the
    wildcard. A deny of the wildcard stops each plain flood at this repeater, which is most
    of the traffic of the mesh.
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
    """The one-line summary of a region for the dialog: its flag, its place, and its notes."""
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
    """Add a region: where (unless the row from which the user started already said), then its name.

    The function has two steps through :func:`~meshterm.ui.menus.run_steps`. Thus Esc on the
    name goes back to the parent picker, with its answer highlighted. The function checks
    the name against the rules of the firmware and against the table. A ``region put`` of a
    name that is already in the table *moves* that region, and this is never what *Add*
    means.
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

    # The name is the last step in both cases. The only difference between a start from a row
    # (one step) and a start from Actions (two steps) is the place of the name in the answers.
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
            help_text="letters, digits, and -, up to 30 bytes",
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
    except RegionNameError:  # pragma: no cover - the validator of the prompt already refused it
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
    """Remove one region, after the red confirm for a single record."""
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
    """Ask before a repeater stops relaying plain floods, which are most of the mesh traffic."""
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
    """Ask before the user leaves edits that are live but not saved. ``True`` means leave."""
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
