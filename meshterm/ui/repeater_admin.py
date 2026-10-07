# SPDX-License-Identifier: Apache-2.0
"""The repeater-admin screens: edit the settings of a remote node over the mesh, in an editor.

These screens are the interactive side of the ``repeater-admin`` tool. First the user
selects a node (in the shared node picker, which shows the nodes with credentials first)
and logs in (with a remembered password, or else with a one-time prompt). Then an editor
opens. Its shape is the same as the Device config screen, on purpose: the same setting,
value, and description lanes, staged ``current → new`` values, and Apply. But it uses the
text CLI of the repeater (refer to :mod:`meshterm.core.remote_config`) instead of the
binary protocol of the companion. Thus it has the settings that only a repeater has, which
the local editor never had: TX delay, Direct TX delay, duty cycle, flood caps, and the
bridge.

Each read is one paced round trip over the mesh. Thus the editor does not read all the
values when it opens. Each value shows what the last read (or the last applied ``set``)
said, from the cache for each node (:class:`~meshterm.core.remote_store.RemoteStore`).
The "Read settings" action refreshes all of them under a progress dialog that the user can
abort, one paced read at a time. ``^R`` (``Read`` on the F-key lane) reads only the
highlighted row again. A setting that the firmware of the node does not have shows as
``n/a``. That is different from ``?``, which means never read. Apply sends the staged
values in the same way, and puts each confirmed value immediately back into the cache.

Other than the settings, the action rows are for the node itself: advert, clock sync,
change of the admin password, and reboot, each behind its own floating confirm dialog.
**Regions** opens the region editor (:mod:`meshterm.ui.region_editor`), which sets the
floods that the node relays, region by region. **Command line** opens the readline-style
remote CLI (:mod:`meshterm.ui.remote_cli`) for all that the catalog does not include.

That command line is also where the page gets more rows. Third-party builds have settings
that the catalog does not know. MeshTerm cannot know which build a node runs unless it
asks the node. Each question is one paced round trip, and each guessed row is a useless
``n/a`` on the page of each other node. Thus MeshTerm probes nothing on speculation. When
the user runs a ``get`` or ``set`` here, and this node answers it, the setting key gets a
row on the page of this node, under **Extra** (:func:`learn_from_cli`). The user used that
round trip anyway, the catalog does not grow, and the page of no other node changes. Only
the user can remove a row, by hand (``Del``). A setting key that stops answering shows
``n/a``, like any other setting key. If it did not, a misread reply would delete the one
thing about this node that nobody else can restore.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.cells import cell_len
from rich.text import Text

from ..core.models import Contact
from ..core.remote_config import (
    DISCOVERED_CATEGORY,
    NEVER_STORED,
    REPEATER_SETTINGS,
    RemoteSetting,
    composite_members,
    composite_reads,
    discovered_setting,
    get_setting,
    learnable_from_get,
    normalize_value,
    parse_reply_value,
    parse_setting_command,
    range_hint,
    read_plan,
    reply_is_error,
    settings_by_category,
    validate_value,
    write_plan,
)
from .menus import (
    PICK_LOCATION_HELP,
    PICK_LOCATION_LABEL,
    confirm_discard,
    exit_rows,
    fit_cells,
    icon_lane,
    lane_header,
    lane_row,
    marked_label,
    menu_rows,
    section_heading,
)
from .trace_screen import TracingDialog
from .tui import Choice, Separator
from .tui.select import DeleteRequest, SelectScreen, splice_hint
from .tui.spinner import Spinner, spinner_interval

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device

#: The time in seconds to wait for one remote reply. Repeaters answer over the mesh, and a
#: route of more than one hop takes seconds. Thus this value is longer on purpose than the
#: value for a local command.
_REPLY_TIMEOUT_S = 10.0

# The sentinels of the menu actions. They are different from the setting keys (CLI
# parameter names).
_CLI = "__cli__"
_LOCATION = "__location__"
_READ = "__read__"
_ADVERT = "__advert__"
_CLOCK = "__clock__"
_PASSWORD = "__password__"
_REBOOT = "__reboot__"
_REGIONS = "__regions__"
_APPLY = "__apply__"
_CANCEL = "__cancel__"

#: The footer atom that names the chord that reads the highlighted setting again.
READ_ONE_HINT = "^R read"

#: The footer atom for the keyboard key that removes a discovered row (only on those rows).
FORGET_HINT = "Del forget"

#: The minimum width of the value lane for a discovered setting. Thus a page whose catalog
#: rows are all ``?`` still shows a part of what the node answered.
_EXTRA_VALUE_MIN = 14


def _extra_value(text: str, width: int) -> str:
    """One discovered value, fitted to the lane: whitespace collapsed, and a long reply cut.

    The value of a discovered setting is whatever the node said. A build can answer with a
    full diagnostic line (``desired=off effective=off supported=yes …``). Without this cut,
    that line would set the width of the value lane (one width for all the rows on the
    page), and push the description of each setting off the edge. Thus the function cuts
    the row here, and the page keeps its shape. The user reads the full reply on the
    command line.
    """
    flat = " ".join(text.split())
    return fit_cells(flat, width) if cell_len(flat) > width else flat


def _spec_for(key: str, cache: dict) -> RemoteSetting | None:
    """The setting that ``key`` names on this node: from the catalog, or discovered there."""
    spec = get_setting(key)
    if spec is not None:
        return spec
    cached = cache.get(key)
    return discovered_setting(key) if cached is not None and cached.discovered else None


def _discovered_specs(cache: dict) -> list[RemoteSetting]:
    """The discovered settings of this node, sorted by setting key: rows the catalog lacks."""
    return [discovered_setting(key) for key, cached in sorted(cache.items()) if cached.discovered]


def _all_specs(cache: dict) -> list[RemoteSetting]:
    """All the settings on the page of this node: the catalog, and what the node told us."""
    return [*REPEATER_SETTINGS, *_discovered_specs(cache)]


def _extra_keys(cache: dict) -> frozenset[str]:
    """The setting keys on the page of this node that came from the node, not the catalog."""
    return frozenset(key for key, cached in cache.items() if cached.discovered)


@dataclass(frozen=True, slots=True)
class ReadOne:
    """The result of the admin menu when ``^R`` asks for the value of one setting again.

    Attributes:
        key: The catalog key of the highlighted setting.
    """

    key: str


class AdminMenu(SelectScreen):
    """The admin editor's list, with a keyboard key that reads the highlighted setting again.

    A full read is one paced round trip for each setting. On a node with all the sections,
    that takes minutes. Thus, to check whether one value was accepted, or to refresh the
    one row that timed out, the user must not have to read all of them. The keyboard key is
    ``^R``, the chord that the app already uses for "retry" (the chat sends an
    unacknowledged message again with it). Here too, it asks the mesh the same question
    again, for the one highlighted row. It must be a chord, because this list filters as
    you type, and each bare letter already has a use.

    The footer names this keyboard key, and the F-key lane draws its ``Read`` chip live,
    only on a setting row that can be read. They never do this on an action row, where
    there is nothing to read.

    It is a full-screen page, not a floating dialog. The full catalog of a node is a place
    where the user works for some time, like the Device config editor that it copies. A box
    that fitted its content, over the node picker, left most of the width of the frame to
    the backdrop. The node picker still floats, and the value prompts, the confirm dialogs,
    and the progress dialogs float over this page.
    """

    floating = False

    #: The setting keys on the page of this node that are not in the catalog. They are
    #: refreshed with the rows.
    extra_keys: frozenset[str] = frozenset()

    def __init__(self, title: str, items: list, **kwargs: Any) -> None:
        """Build the list of the page. Its rows end at the edge, and ←→ does not scroll them.

        This is the same as on the Device config page, on purpose. The Actions rows pin a
        head block (:func:`~meshterm.ui.menus.menu_rows`). That alone turns on the ←→
        scroll for the full list. Then a setting row, which pins nothing, moved its label
        off the screen with its description, and the footer got a ``←→ scroll`` atom.
        ``hscroll=False`` keeps each row complete and cut with the ellipsis, like each other
        editor lane.
        """
        kwargs.setdefault("hscroll", False)
        super().__init__(title, items, **kwargs)

    def _row_key(self) -> str | None:
        """The highlighted row's setting key, or ``None`` on an action row."""
        current = self._current_choice()
        if current is None or not isinstance(current.value, str):
            return None
        return current.value

    def _readable_key(self) -> str | None:
        """The setting key of the highlighted row, if the node can be asked for that setting."""
        key = self._row_key()
        if key is None:
            return None
        if key in self.extra_keys:
            return key  # a discovered row is a string setting, and can always be read again
        spec = get_setting(key)
        return spec.key if spec is not None and spec.readable else None

    def _forgettable_key(self) -> str | None:
        """The setting key of the highlighted row, if it is a discovered row that Del removes."""
        key = self._row_key()
        return key if key is not None and key in self.extra_keys else None

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The list's own hint, plus the read atom while the highlight is on a readable setting."""
        base = super().footer_hint
        return splice_hint(base, READ_ONE_HINT) if self._readable_key() else base

    @property
    def picocalc_lyra_lane(self):
        """The list's lane, with ``Read`` on F3 and ``Forget`` behind it: dim where inactive.

        F1/F2 are the section jumps of this grouped list, and F4/F5 are the pager. F3 is the
        slot that a select list uses for its own verb. Read and Forget are the two halves of
        the same slot, because they have the same subject (this row). They are never both
        live: a catalog row can be read but not forgotten, and only a discovered row can be
        removed. On an action row, they are dim, not empty: the two actions exist on this
        screen, but not for the highlighted row.
        """
        from .tui.fkeys import FPair

        lane = list(super().picocalc_lyra_lane)
        lane[2] = FPair(
            "Read",
            "retry",
            opp_label="Forget",
            opp_action="delete",
            enabled=self._readable_key() is not None,
            opp_enabled=self._forgettable_key() is not None,
        )
        return lane

    def handle(self, action: str, data: str = "") -> None:
        """Ask for the value of the highlighted setting, or act as each select list does."""
        if action == "retry":
            key = self._readable_key()
            if key is not None:
                self.resolve(ReadOne(key))
            return
        super().handle(action, data)


async def open_repeater_admin(ctx: AppContext) -> dict[str, Any] | None:
    """Run the repeater-admin flow: select a node, log in, and administer it.

    The node picker is a dialog: a question before the user goes in, which closes when it
    is answered. Thus the admin page opens over the main menu, and Esc on the page goes
    back there: the page is the full visit. If the node refuses the login (or never
    answers), the picker asks again, with the same node highlighted. Thus a mistyped
    password costs one Enter and a retry, not a new start of the flow. The list once
    stayed pushed below the page as a hub. That looked like two places where there is only
    one.

    Args:
        ctx: The shared application context (it must run the interactive TUI surface).

    Returns:
        A summary of the administered node (for the log of the tool), or ``None`` if the
        user left the picker and never started a session.

    Raises:
        RuntimeError: If it is called outside the interactive menu (no full-screen
            session).
    """
    from .admin_picker import pick_admin_node
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the menu-only caller prevents this
        raise RuntimeError("repeater admin is only available in the menu")

    device = await ctx.device()
    # Through the session cache: when this screen opens, it must not read the (slow)
    # contacts table again if another screen already read it (refer to
    # :class:`~meshterm.services.device_state.DeviceState`). The device handle above is
    # still necessary for the admin login and for the CLI session that come after.
    contacts = await ctx.devstate.contacts()
    node: Contact | None = None
    while True:
        node = await pick_admin_node(
            ctx,
            contacts,
            title="Repeater admin — node to manage",
            prompt="The remote node to set up (you need its admin password):",
            default=node,
        )
        if node is None:
            return None
        if await _login(ctx, device, node):
            return await _admin_session(ctx, device, node)


async def _login(ctx: AppContext, device: Device, node: Contact) -> bool:
    """Log in to ``node``: with a remembered password silently, or else one floating prompt.

    MeshTerm remembers a password that works. It forgets a rejected password, so that the
    next try asks for a new one (the convention for remote admin in all the app). But it
    forgets only a rejected password. A node that never answered said nothing about the
    password, so when there is no answer, MeshTerm keeps the password. Before, if the user
    administered a repeater that was down at that time, MeshTerm erased its credential.
    """
    from ..core.models import LoginResult

    session = ctx.ui.session
    password = ctx.admin_store.get(node)
    prompted = password is None
    if password is None:
        password = await session.text(
            f"Admin password for {node.name}",
            prompt="The node ignores admin commands without a login.",
            password=True,
            # The picker dialog is closed now, so this prompt is the only screen on the stack.
            # The flag keeps it a box over a blank base, not a full-frame prompt.
            floating=True,
        )
        if not password:
            return False
    async with ctx.ui.busy_overlay():
        outcome = await device.admin_login(node, password)
    ctx.admin_store.record(node, password, outcome)
    if outcome is LoginResult.REFUSED:
        await session.message_dialog(
            Text(
                f"{node.name!r} rejected the admin login (wrong password?). "
                + ("" if prompted else "The saved password was cleared; ")
                + "try again to enter a new one.",
                style="err",
            ),
            title="Admin login",
        )
        return False
    if not outcome:
        await session.message_dialog(
            Text(
                f"No reply from {node.name} — it may be out of reach, asleep, or busy. "
                + ("" if prompted else "The saved password was kept; ")
                + "try again when it answers.",
                style="warn",
            ),
            title="Admin login",
        )
        return False
    return True


async def _admin_session(ctx: AppContext, device: Device, node: Contact) -> dict[str, Any]:
    """Run the editor loop for one node that the user is logged in to.

    One screen for the full session. Its rows refresh in place after each action: they
    hold the ``current → new`` values of the cache, and the title counts the staged values.
    Thus the content changes below a highlight that stays where the user put it, and the
    typed filter stays too. Each sub-prompt floats over the screen, and Esc leaves the
    node.
    """
    from .tui import CANCEL

    session = ctx.ui.session

    pending: dict[str, str] = {}  # setting key -> staged new value
    applied = 0

    cache = ctx.remote_store.settings(node)
    title, items = _menu_items(node, cache, pending)
    menu = AdminMenu(
        title,
        items,
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
        delete_hint=FORGET_HINT,
    )
    menu.extra_keys = _extra_keys(cache)
    async with session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            if choice is CANCEL:
                choice = _CANCEL

            if choice in (None, _CANCEL):
                if pending and not await confirm_discard(ctx, len(pending), verb="sending"):
                    continue
                return {"node": node.name, "applied": applied}
            if isinstance(choice, ReadOne):
                spec = _spec_for(choice.key, cache)
                if spec is not None:
                    await read_settings(ctx, device, node, [spec])
            elif isinstance(choice, DeleteRequest):
                await _forget_discovered(ctx, node, str(choice.value), pending)
            elif choice == _APPLY:
                applied += await _apply(ctx, device, node, pending)
            elif choice == _READ:
                # The page of this node, not the catalog: the same sweep refreshes a
                # discovered row and all the other rows, and the row shows n/a if the node
                # stops answering.
                await read_settings(ctx, device, node, _all_specs(cache))
            elif choice == _LOCATION:
                await _stage_location(ctx, cache, pending)
            elif choice == _CLI:
                await _command_line(ctx, device, node)
            elif choice == _REGIONS:
                from .region_editor import open_region_editor

                await open_region_editor(ctx, device, node)
            elif choice == _ADVERT:
                await _simple_action(
                    ctx,
                    device,
                    node,
                    "advert",
                    title="Send advert",
                    prompt=f"Ask {node.name} to announce itself (flood) now?",
                    commit="Send",
                )
            elif choice == _CLOCK:
                await _simple_action(
                    ctx,
                    device,
                    node,
                    "clock sync",
                    title="Sync clock",
                    prompt=f"Set {node.name}'s clock from this companion's time?",
                    commit="Sync",
                )
            elif choice == _PASSWORD:
                await _change_password(ctx, device, node)
            elif choice == _REBOOT:
                await _simple_action(
                    ctx,
                    device,
                    node,
                    "reboot",
                    title="Reboot node",
                    prompt=f"Reboot {node.name} now? It drops off the mesh while booting.",
                    commit="Reboot",
                    danger=True,
                )
            else:  # a setting key
                spec = _spec_for(str(choice), cache)
                if spec is not None and not spec.writable:
                    # A read-only fact has nothing to stage. Enter can only ask for it again.
                    await read_settings(ctx, device, node, [spec])
                else:
                    await _stage_setting(ctx, node, str(choice), cache, pending)
            # A read or an apply writes the cache that the rows come from again. Read the
            # cache again, so that the values are the values that the action produced.
            cache = ctx.remote_store.settings(node)
            title, items = _menu_items(node, cache, pending)
            menu.extra_keys = _extra_keys(cache)
            menu.replace_items(items, title=title)


# --- the menu ------------------------------------------------------------------------


def _menu_items(node: Contact, cache: dict, pending: dict[str, str]) -> tuple[str, list]:
    """Build the title and the rows of the editor menu from the cache and the staged values.

    The lane layout is the same as on the Device config editor: setting, value (with any
    staged ``→ new``), and description, below one header line. Thus the administration of
    a remote node looks exactly like the edit of the settings of the local one.
    """
    sections: list[tuple[str, list[tuple[str, Text, str, Any]]]] = []
    for category, specs in settings_by_category():
        rows: list[tuple[str, Text, str, Any]] = []
        for spec in specs:
            # In the words of the node that the page is open on: on a room server, the
            # guest password is the room password (RemoteSetting.for_node).
            spec = spec.for_node(node.node_type)
            if spec.key == "lat":
                # A position selected on the map sets the two coordinates at the same time.
                # Thus its row is first, above the pair that it fills: the same row, at the
                # same place, as on the Device config page.
                rows.append((PICK_LOCATION_LABEL, Text(), PICK_LOCATION_HELP, _LOCATION))
            rows.append((spec.label, _value_text(spec, cache, pending), spec.help, spec.key))
        sections.append((category, rows))

    # What this node told us itself, at the end and in its own section. The section is not
    # there on a node that told us nothing, which is each node until a user asks it
    # something. Its values fit in the lane that the catalog rows already need. The value
    # lane has one width for the full page. Thus a reply with no limit here would push the
    # description of each setting to the right, off the edge of a 72-cell screen. It would
    # also change the shape of a page that the user opened to read the catalog.
    extra = _discovered_specs(cache)
    if extra:
        budget = max(
            _EXTRA_VALUE_MIN,
            max((cell_len(value.plain) for _, rows in sections for _, value, _, _ in rows)),
        )
        sections.append(
            (
                DISCOVERED_CATEGORY,
                [(s.label, _value_text(s, cache, pending, budget), s.help, s.key) for s in extra],
            )
        )

    label_w = max(cell_len(label) for _, rows in sections for label, _, _, _ in rows)
    value_w = max(cell_len(value.plain) for _, rows in sections for _, value, _, _ in rows)

    # The same pinned header, which fits itself, as the header above the lanes of the local
    # editor. It stays on the screen with the category headings for the full list. When a
    # long cached value leaves no space for the last label, the header abbreviates, and it
    # does not wrap (menus.lane_header).
    items: list = [Separator(lambda w: lane_header(label_w, value_w, w), pinned=True)]
    for category, rows in sections:
        items.append(section_heading(category))
        for label, value, help_text, key in rows:
            items.append(
                Choice(
                    title=lane_row(label, value, help_text, label_w, value_w),
                    value=key,
                    # Only a discovered row can be removed. A catalog row that goes away
                    # would come back at the next paint, because the catalog still names it.
                    deletable=category == DISCOVERED_CATEGORY,
                )
            )

    # ↻ and ⌨ are one cell wide, but 📡 🕒 🔐 🔄 are two. Thus the icon lane is measured one
    # time, and each icon is padded to that width. If not, the labels of Read settings and
    # Command line would start one cell to the left of the rows below them.
    actions = [
        ("↻", "Read settings", "Fetch every value from the node, one paced read", _READ),
        ("⌨", "Command line…", "Talk to the node's CLI directly", _CLI),
        ("🔖", "Regions…", "Which scoped floods it relays", _REGIONS),
        ("📡", "Send advert…", "Have the node announce itself now", _ADVERT),
        ("🕒", "Sync clock…", "Set the node's clock over the mesh", _CLOCK),
        ("🔐", "Admin password…", "Change the node's admin password", _PASSWORD),
        ("🔄", "Reboot node…", "Restart it remotely", _REBOOT),
    ]
    lane = icon_lane(icon for icon, _, _, _ in actions)
    items.append(section_heading("Actions"))
    items.extend(
        menu_rows(
            (marked_label(icon, label, "", lane=lane), help_text, value)
            for icon, label, help_text, value in actions
        )
    )

    staged = len(pending)
    items.extend(exit_rows(staged, apply_value=_APPLY, back_value=_CANCEL))

    title = f"Repeater admin — {node.name}" + (f" · {staged} staged" if staged else "")
    return title, items


def _value_text(
    spec: RemoteSetting, cache: dict, pending: dict[str, str], width: int = _EXTRA_VALUE_MIN
) -> Text:
    """The VALUE lane of one setting: what the node said last, and any staged arrow.

    There are five states, each with its own word:

    - The value.
    - ``empty``, for a string that the node holds blank.
    - ``n/a``, when the node answered with something that cannot be the value of this
      setting (its firmware has no such setting).
    - ``?``, when the setting was never read (or the node never answered).
    - ``write-only``, for a setting that the node cannot be asked for.

    No age: the lane is the value, and a time stamp beside it only made the lane too full.
    """
    cached = cache.get(spec.key)
    if not spec.readable:
        value = Text("write-only", style="faint")
    elif cached is None:
        value = Text("?", style="muted")
    elif not cached.supported:
        value = Text("n/a", style="muted")
    elif cached.value == "":
        value = Text("empty", style="muted")
    elif spec.discovered:
        value = Text(_extra_value(cached.value, width))
    else:
        value = Text(spec.display(cached.value))
    if spec.key in pending:
        staged = pending[spec.key]
        shown = _extra_value(staged, width) if spec.discovered else spec.display(staged)
        value.append(f" → {shown}", style="warn")
    return value


# --- staging and applying ---------------------------------------------------------


async def _stage_setting(
    ctx: AppContext, node: Contact, key: str, cache: dict, pending: dict[str, str]
) -> None:
    """Prompt for the new value of one setting, and stage it (MeshTerm sends nothing yet).

    The prompt names the setting as its row does, in the words of the node that it is for.
    """
    spec = _spec_for(key, cache)
    if spec is None or not spec.writable:  # pragma: no cover - the menu offers only real settings
        return
    spec = spec.for_node(node.node_type)
    cached = cache.get(key)
    known = cached.value if cached is not None and cached.supported else None
    current = pending.get(key, known or "")

    if spec.kind == "bool":
        picked = await ctx.ui.dialog(
            spec.help,
            [("Off", "off"), ("On", "on")],
            title=spec.label,
            default=1 if current == "on" else 0,
            keys={"0": "off", "1": "on", "n": "off", "y": "on"},
        )
        if picked is None:
            return
        value = str(picked)
    elif spec.kind == "enum":
        items = [
            Choice(
                title=option.label
                + (f" — {option.help}" if option.help else "")
                + ("  (current)" if option.value == current else ""),
                value=option.value,
            )
            for option in spec.options
        ]
        picked = await ctx.ui.select(
            spec.label,
            items,
            prompt=spec.help,
            default=current if any(o.value == current for o in spec.options) else None,
        )
        if picked is None:
            return
        value = str(picked)
    else:
        raw = await ctx.ui.text(
            spec.label,
            prompt=spec.help,
            default=str(current),
            validate=lambda t: validate_value(spec, t),
            help_text=range_hint(spec),
        )
        if raw is None:
            return
        value = normalize_value(spec, raw)

    if value == known:
        pending.pop(key, None)  # the same as what the node said last, so nothing to send
    else:
        pending[key] = value


async def _forget_discovered(
    ctx: AppContext, node: Contact, key: str, pending: dict[str, str]
) -> None:
    """Remove one discovered row from the page of this node, after a confirm dialog.

    This is the only way to remove a discovered row. A read could remove one, because a
    setting key that the node no longer answers can be real (after a reused identity moves
    to a different board). But then one misread reply would silently delete the row: a
    truncated line, a node that answers during a reboot, or a stray reply that MeshTerm
    matched to the wrong command. When that occurs on a catalog row, the cost is an
    ``n/a``, and nothing is lost, because the catalog still names the setting key. But
    only this page knows the setting key of a discovered row, and the user may not remember
    it. Thus a read can only show the same ``n/a``, and a removal is a decision.

    It is a red confirm dialog, like each other delete of one record, but nothing changes
    on the node. What goes is what MeshTerm remembers. If the user asks for the setting key
    again, the row comes back.
    """
    from .tui import CANCEL

    choice = await ctx.ui.dialog(
        f"Stop showing {key} on {node.name}? Nothing on the node changes — "
        "ask it for the setting again and the row comes back.",
        [("Cancel", CANCEL), ("Forget", "forget")],
        title="Forget setting",
        default=1,
        destructive=True,
    )
    if choice != "forget":
        return
    pending.pop(key, None)
    ctx.remote_store.forget_setting(node, key)


async def _stage_location(ctx: AppContext, cache: dict, pending: dict[str, str]) -> None:
    """Select the advertised location of the node on the map, and stage the two coordinates.

    This is the row of the Device config page, but it uses the CLI of this node. The map
    opens on the staged position (or on the position that the node said last), or on the
    mesh if there is no position. Each selected coordinate is put in the same format as a
    read of it. Thus, if the user selects the position that the node already holds, the
    staged value is removed, and no ``set`` that does nothing goes in the queue. MeshTerm
    sends nothing until Apply.
    """
    from .map_screen import coords_or_none, pick_location

    def known(key: str) -> str | None:
        cached = cache.get(key)
        return cached.value if cached is not None and cached.supported else None

    lat = pending.get("lat", known("lat"))
    lon = pending.get("lon", known("lon"))
    picked = await pick_location(ctx, initial=coords_or_none(lat, lon))
    if picked is None:
        return
    for key, value in zip(("lat", "lon"), picked, strict=True):
        spec = get_setting(key)
        if spec is None:  # pragma: no cover - both keys are in the catalog
            continue
        # Six decimals ≈ 0.1 m: more than the precision of the map, and enough for an advert.
        text = normalize_value(spec, f"{value:.6f}")
        if text == known(key):
            pending.pop(key, None)  # what the node already says, so nothing to send
        else:
            pending[key] = text


def _known_values(ctx: AppContext, node: Contact) -> dict[str, str]:
    """The last values read from the node, to state the unstaged fields of a composite again."""
    return {
        key: cached.value
        for key, cached in ctx.remote_store.settings(node).items()
        if cached.supported
    }


def _remark(reply: str, value: str) -> str:
    """What the reply to a successful write adds after ``OK`` (``reboot to apply``), or ``""``."""
    remark = re.sub(r"^ok\b[\s,:-]*", "", reply.strip(), flags=re.IGNORECASE)
    return "" if remark.lower() in ("", value.lower()) else remark


async def _apply(ctx: AppContext, device: Device, node: Contact, pending: dict[str, str]) -> int:
    """Send all the staged values, paced, under a progress dialog that the user can abort.

    MeshTerm sends the staged values in the groups of
    :func:`~meshterm.core.remote_config.write_plan`: one command for each setting, but the
    four fields of the radio go together as one ``set radio``. That command states each
    field again. Thus, if a radio field is staged and its sibling fields were never read,
    MeshTerm reads them first (one paced ``get radio``), and does not guess a frequency.
    Each confirmed value goes immediately into the cache for the node, and MeshTerm forgets
    each other spelling of the same firmware value. A write that the node rejects or does
    not answer stays staged, so that the user can try it again (or unstage it). It is not
    silently removed. The function stores one ``runs`` row for the batch.

    Returns:
        The number of settings that the node accepted.
    """
    session = ctx.ui.session
    run_id = ctx.repo.start_run(
        "repeater-admin",
        {"node": node.name, "mode": "apply", "settings": sorted(pending)},
        ctx.profile_name,
    )
    outcomes: list[Text] = []
    applied = 0

    async def work(dialog: TracingDialog) -> None:
        nonlocal applied
        first = True

        async def send(command: str, status: str) -> str | None:
            nonlocal first
            if not first:  # each transmission after the first waits for the cooldown
                await asyncio.sleep(ctx.preferences.trace_cooldown_s)
            first = False
            dialog.status = status
            session.invalidate()
            return await device.send_remote_command(node, command, timeout=_REPLY_TIMEOUT_S)

        for command in composite_reads(pending, _known_values(ctx, node)):
            fills = [s for s in REPEATER_SETTINGS if s.get_command == command]
            reply = await send(command, command)
            if reply is not None:
                remember_reply(ctx, node, fills, reply)

        writes = write_plan(pending, _known_values(ctx, node))
        # The catalog, plus the discovered rows of this node. A staged setting key that only
        # this node has is still a row, with a label to name in the outcome and a write to
        # count.
        specs = _all_specs(ctx.remote_store.settings(node))
        for i, write in enumerate(writes, start=1):
            staged = [s for s in specs if s.key in write.values and s.key in pending]
            names = ", ".join(f"{s.label} = {s.display(write.values[s.key])}" for s in staged)
            if write.missing:
                unread = ", ".join(s.label for s in map(get_setting, write.missing) if s)
                outcomes.append(
                    Text.assemble(
                        ("✗ ", "err"), f"{names} — {unread} couldn't be read (still staged)"
                    )
                )
                continue
            verb = " ".join(write.command.split()[:2])  # never the value of a secret on screen
            reply = await send(write.command, f"{verb} · {i}/{len(writes)}")
            if reply is not None and not reply_is_error(reply):
                for key, value in write.values.items():
                    ctx.remote_store.remember_setting(node, key, value)
                    spec = get_setting(key)
                    for other in spec.overlaps if spec is not None else ():
                        ctx.remote_store.forget_setting(node, other)
                for spec in staged:
                    pending.pop(spec.key, None)
                applied += len(staged)
                remark = _remark(reply, write.values[staged[0].key]) if staged else ""
                outcomes.append(
                    Text.assemble(("✓ ", "ok"), names, (f" — {remark}" if remark else "", "muted"))
                )
            elif reply is None:
                outcomes.append(Text.assemble(("? ", "warn"), f"{names} — no reply (still staged)"))
            else:
                outcomes.append(
                    Text.assemble(("✗ ", "err"), f"{names} — {reply.strip()} (still staged)")
                )

    aborted = await _run_under_dialog(ctx, f"Applying — {node.name}", work)
    ctx.repo.finish_run(
        run_id,
        "error" if aborted else "ok",
        {"applied": applied, "staged_left": len(pending)},
    )
    summary = Text.assemble(
        (f"{applied}", "brand"), f" of {applied + len(pending)} settings applied"
    )
    if aborted:
        summary.append("  (aborted — the rest stay staged)", style="warn")
    await ctx.ui.session.message_dialog(
        Text("\n").join([summary, Text(), *outcomes]) if outcomes else summary,
        title=f"Apply — {node.name}",
    )
    return applied


def remember_reply(ctx: AppContext, node: Contact, fills: list[RemoteSetting], reply: str) -> int:
    """Put the reply of one read into the cache, for each setting that it answers.

    A reply that cannot be the value of the setting means that the node has no such
    setting. MeshTerm remembers it as that (the row shows ``n/a``). This applies to an error
    (``??: key``, ``Error: unsupported``) and also to an answer to a different question. The
    second type is how older firmware says it. ``get`` matches its setting keys by prefix.
    Thus, on v1.15, a setting key that the firmware does not have goes into the branch of a
    shorter sibling key: ``get radio.fem.rxgain`` answers with the ``> 910.525,62.5,7,5``
    of ``get radio``. Also, ``gps`` on a board with no GPS receiver answers
    ``Can't find GPS``. Before, these rows stayed on ``?``, which means never asked. Now
    only a read that got no reply at all (which this function does not get) keeps ``?``.

    Returns:
        The number of settings that got a value.
    """
    got = 0
    for spec in fills:
        value = parse_reply_value(spec, reply)  # None for an error reply too
        if value is None:
            ctx.remote_store.remember_unsupported(node, spec.key)
        else:
            ctx.remote_store.remember_setting(node, spec.key, value)
            got += 1
    return got


def learn_from_cli(ctx: AppContext, node: Contact, command: str, reply: str) -> None:
    """Put what one exchange on the command line proved about ``node`` into its page.

    The command line is the only place where the user can reach a setting outside the
    catalog. Thus it is also the only place where MeshTerm can find one. A ``get`` or
    ``set`` that this node answered proves that the setting key exists. The row that the
    setting key gets costs no round trip that the user did not ask for. The catalog does not
    grow: a setting key learned here belongs only to this node (refer to
    :func:`~meshterm.core.remote_config.discovered_setting`), and the page of each other
    node stays as it was.

    There are three cases, in the order of the tests:

    * The **own key of a composite** (``get radio``) fills all four of its fields, exactly
      as a read of any one of them does.
    * A **catalog key** goes into the cache in the same way as from the sweep or an Apply.
      That is also why ``get tx`` on the command line no longer leaves the TX power row out
      of date.
    * Each other setting key is **discovered**, and the two verbs are not equally good
      evidence. ``handleSetCmd`` matches a setting key with its trailing space, and refuses
      an unknown setting key immediately. Thus an accepted write is proof. ``handleGetCmd``
      matches on a bare prefix, and can answer a setting key that it does not have from the
      branch of a shorter setting key. Thus a read is proof only when the reply cannot be
      the reply for that shorter setting key
      (:func:`~meshterm.core.remote_config.learnable_from_get`).

    MeshTerm never stores ``prv.key``, however the user asked for it
    (:data:`~meshterm.core.remote_config.NEVER_STORED`).
    """
    parsed = parse_setting_command(command)
    if parsed is None or parsed.key in NEVER_STORED:
        return
    members = composite_members(parsed.key)
    spec = _spec_for(parsed.key, ctx.remote_store.settings(node))

    if parsed.verb == "get":
        if members:
            remember_reply(ctx, node, members, reply)
        elif spec is not None:
            remember_reply(ctx, node, [spec], reply)
        elif not reply_is_error(reply) and learnable_from_get(
            parsed.key, reply, _known_values(ctx, node)
        ):
            found = discovered_setting(parsed.key)
            value = parse_reply_value(found, reply)
            if value is not None:
                ctx.remote_store.remember_discovered(node, found.key, value)
        return

    if reply_is_error(reply):
        return  # a refused write tells nothing about the setting key or its value
    if members:
        for member in members:
            value = parse_reply_value(member, parsed.value)
            if value is not None:
                ctx.remote_store.remember_setting(node, member.key, value)
        return
    if spec is not None and not spec.discovered:
        value = parse_reply_value(spec, parsed.value)
        if value is None:
            return  # the catalog cannot read back these words, so keep the row as it was
        ctx.remote_store.remember_setting(node, spec.key, value)
        for other in spec.overlaps:
            ctx.remote_store.forget_setting(node, other)
        return
    ctx.remote_store.remember_discovered(node, parsed.key, parsed.value)


async def read_settings(
    ctx: AppContext, device: Device, node: Contact, specs: Iterable[RemoteSetting]
) -> None:
    """Refresh ``specs`` from the node, one paced read at a time.

    The four radio fields share one ``get radio`` (refer to
    :func:`~meshterm.core.remote_config.read_plan`), so a full read asks each question one
    time. Values that parse go into the cache (and onto the screen). A setting that the
    firmware does not have shows ``n/a``. An abort keeps all the values that were already
    read. After the reads, a dialog lists the reads that the node never answered. Their
    rows continue to show what they said last, and without the list, they would look new.
    """
    session = ctx.ui.session
    plan = read_plan(specs)
    run_id = ctx.repo.start_run(
        "repeater-admin", {"node": node.name, "mode": "read"}, ctx.profile_name
    )
    read = 0
    unanswered: list[str] = []

    async def work(dialog: TracingDialog) -> None:
        nonlocal read
        for i, (command, fills) in enumerate(plan, start=1):
            dialog.status = f"{command} · {i}/{len(plan)}"
            session.invalidate()
            reply = await device.send_remote_command(node, command, timeout=_REPLY_TIMEOUT_S)
            if reply is None:
                unanswered.extend(spec.label for spec in fills)
            else:
                read += remember_reply(ctx, node, fills, reply)
            if i < len(plan):
                await asyncio.sleep(ctx.preferences.trace_cooldown_s)

    aborted = await _run_under_dialog(ctx, f"Reading — {node.name}", work)
    asked = sum(len(fills) for _command, fills in plan)
    ctx.repo.finish_run(
        run_id,
        "error" if aborted else "ok",
        {"read": read, "asked": asked, "unanswered": len(unanswered)},
    )
    if unanswered and not aborted:
        await session.message_dialog(
            Text(
                f"No reply from {node.name} for: {', '.join(unanswered)}. "
                "Those rows still show what they last said.",
                style="warn",
            ),
            title=f"Read — {node.name}",
        )


async def _run_under_dialog(ctx: AppContext, title: str, work) -> bool:
    """Run an async remote batch under a floating spinner dialog with Abort.

    Args:
        ctx: The shared application context.
        title: The heading of the dialog.
        work: ``async work(dialog)``, which does the batch and updates ``dialog.status``.

    Returns:
        ``True`` if the user aborted, ``False`` if the batch ran to the end.
    """
    session = ctx.ui.session
    spinner = Spinner()
    dialog = TracingDialog(title, spinner=spinner, on_abort=lambda: None)
    dialog.show_last = False

    task = asyncio.ensure_future(work(dialog))
    dialog.on_abort = task.cancel

    async def animate() -> None:
        while True:
            await asyncio.sleep(spinner_interval())
            spinner.tick()
            session.invalidate()

    ticker = asyncio.ensure_future(animate())
    session.push(dialog)
    aborted = False
    try:
        await task
    except asyncio.CancelledError:
        aborted = True
    except Exception as exc:  # noqa: BLE001 - show it in a dialog, and keep the screen
        await session.message_dialog(Text(str(exc), style="err"), title=title)
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - a small spinner fault must never stop the batch
            pass
        session.pop(dialog)
    return aborted


# --- one-shot actions ------------------------------------------------------------


async def _simple_action(
    ctx: AppContext,
    device: Device,
    node: Contact,
    command: str,
    *,
    title: str,
    prompt: str,
    commit: str,
    danger: bool = False,
) -> None:
    """Confirm and send one fixed CLI command, then show the reply of the node."""
    choice = await ctx.ui.dialog(
        prompt,
        [("Cancel", None), (commit, "go")],
        title=title,
        default=1,
        danger=danger,
    )
    if choice != "go":
        return
    async with ctx.ui.busy_overlay():
        reply = await device.send_remote_command(node, command, timeout=_REPLY_TIMEOUT_S)
    if reply is None:
        body = Text("no reply — the command may still have landed", style="warn")
    else:
        body = Text(reply.strip(), style="err" if reply_is_error(reply) else "ok")
    await ctx.ui.session.message_dialog(body, title=title)


async def _change_password(ctx: AppContext, device: Device, node: Contact) -> None:
    """Change the admin password of the node (and remember the new password if it succeeds)."""
    new = await ctx.ui.text(
        f"New admin password for {node.name}",
        prompt="Sent over the mesh; the node applies it immediately.",
        password=True,
    )
    if not new:
        return
    confirmed = await ctx.ui.dialog(
        f"Change {node.name}'s admin password now? You will need the new one everywhere.",
        [("Cancel", None), ("Change", "go")],
        title="Admin password",
        default=1,
        danger=True,
    )
    if confirmed != "go":
        return
    async with ctx.ui.busy_overlay():
        reply = await device.send_remote_command(node, f"password {new}", timeout=_REPLY_TIMEOUT_S)
    if reply is not None and not reply_is_error(reply):
        ctx.admin_store.remember(node, new)  # the password that works is now the new one
        body = Text("✓ password changed and remembered", style="ok")
    elif reply is None:
        body = Text(
            "no reply — the change may or may not have landed; the old password "
            "stays remembered until a login proves otherwise",
            style="warn",
        )
    else:
        body = Text(reply.strip(), style="err")
    await ctx.ui.session.message_dialog(body, title="Admin password")


# --- the command line ---------------------------------------------------------------


async def _command_line(ctx: AppContext, device: Device, node: Contact) -> None:
    """Open the readline-style remote CLI for ``node`` (the history is stored for each node)."""
    from .remote_cli import RemoteCliScreen

    session = ctx.ui.session
    worker: asyncio.Task | None = None
    ticker: asyncio.Task | None = None

    def send(command: str) -> None:
        nonlocal worker
        if worker is not None and not worker.done():
            return  # one command at a time, because each send is a transmission
        screen.sent(command)
        ctx.remote_store.append_history(node, command)
        worker = asyncio.ensure_future(roundtrip(command))

    async def roundtrip(command: str) -> None:
        try:
            reply = await device.send_remote_command(node, command, timeout=_REPLY_TIMEOUT_S)
        except asyncio.CancelledError:
            screen.failed("cancelled", error=False)
            raise
        except Exception as exc:  # noqa: BLE001 - shown inline, and the screen stays open
            screen.failed(str(exc), error=True)
            return
        if reply is None:
            screen.failed(f"no reply within {_REPLY_TIMEOUT_S:.0f} s")
        else:
            learn_from_cli(ctx, node, command, reply)
            screen.reply(reply)

    screen = RemoteCliScreen(
        node_label=node.name,
        history=ctx.remote_store.history(node),
        send=send,
        session=session,
        extra_keys=_extra_keys(ctx.remote_store.settings(node)),
    )

    async def animate() -> None:
        while True:
            await asyncio.sleep(spinner_interval())
            if screen.busy:
                screen.tick()
                session.invalidate()

    ticker = asyncio.ensure_future(animate())
    try:
        await session.run_screen(screen)
    finally:
        for task in (worker, ticker):
            if task is not None and not task.done():
                task.cancel()
        for task in (worker, ticker):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - the screen is closed, so nothing to show
                    pass
