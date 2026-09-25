# SPDX-License-Identifier: Apache-2.0
"""The repeater-admin screens: configure a remote node over the mesh, editor-style.

The interactive face of the ``repeater-admin`` tool. Picking a node (the shared
credentialed-first picker) and logging in (remembered passwords, the one-time prompt
otherwise) opens an editor deliberately shaped like the local device-configuration
screen — the same setting / value / description lanes, staged ``current → new`` values,
Apply — but speaking the repeater's text CLI (see :mod:`meshterm.core.remote_config`)
instead of the companion's binary protocol, which brings the repeater-only knobs the
local editor never had: TX delay, Direct TX delay, duty cycle, flood caps, the bridge.

Because every read is one paced mesh round trip, the editor never bulk-reads on open:
values show what the last read (or the last applied set) said, from the per-node cache
(:class:`~meshterm.core.remote_store.RemoteStore`). The *Read settings* action refreshes
every one of them under an abortable progress dialog, one paced read at a time, and
``^R`` (``Read`` on the F-key lane) re-reads just the highlighted row. A setting the node's
firmware doesn't have reads as ``n/a`` — kept apart from ``?``, never read. Apply sends the
staged values the same way and folds each confirmed value straight back into the cache.

Beyond the settings, the action rows cover the box itself — advert, clock sync, change
admin password, reboot, each behind its own floating confirmation — **Regions** opens the
region editor (:mod:`meshterm.ui.region_editor`), which floods it relays region by region,
and the **Command line** opens the readline-style remote CLI (:mod:`meshterm.ui.remote_cli`)
for anything the catalog doesn't spell.

That command line is also where the page *grows*. Third-party builds carry settings the
catalog has never heard of, and MeshTerm can't know which build a node is running without
asking it — every question being one paced round trip, and every guessed row a dead ``n/a``
on everyone else's page. So nothing is probed speculatively: a ``get``/``set`` the reader
ran here, which *this node* answered, earns a row on *this node's* page under **Extra**
(:func:`learn_from_cli`). The round trip was one the reader was spending anyway, the catalog
doesn't grow, and no other node's page changes. A row is only ever removed by hand
(``Del``): a key that stops answering reads ``n/a`` like any other, because a misread reply
would otherwise delete the one thing about this node nobody else can restore.
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

#: Seconds to wait for one remote reply. Repeaters answer over the mesh — multi-hop
#: routes take seconds — so this is deliberately more patient than a local command.
_REPLY_TIMEOUT_S = 10.0

# Menu action sentinels (distinct from setting keys, which are CLI parameter names).
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

#: The footer atom naming the chord that re-reads the highlighted setting.
READ_ONE_HINT = "^R read"

#: The footer atom naming the key that drops a discovered row (surfaced on those rows only).
FORGET_HINT = "Del forget"

#: The narrowest the value lane may be squeezed to for a discovered setting — a floor, so a
#: page whose catalog rows are all ``?`` still shows something of what the node answered.
_EXTRA_VALUE_MIN = 14


def _extra_value(text: str, width: int) -> str:
    """One discovered value, fitted for the lane: whitespace collapsed, a long reply cut.

    A discovered setting's value is whatever the node said, and a build that answers a whole
    diagnostic line (``desired=off effective=off supported=yes …``) would otherwise set the
    value lane's width — one width, for every row on the page — and push each setting's
    description off the edge. The row is cut here instead of the page being reshaped around
    it; the command line is where the whole reply is read.
    """
    flat = " ".join(text.split())
    return fit_cells(flat, width) if cell_len(flat) > width else flat


def _spec_for(key: str, cache: dict) -> RemoteSetting | None:
    """The setting ``key`` names on this node: the catalog's, or its own discovered one."""
    spec = get_setting(key)
    if spec is not None:
        return spec
    cached = cache.get(key)
    return discovered_setting(key) if cached is not None and cached.discovered else None


def _discovered_specs(cache: dict) -> list[RemoteSetting]:
    """This node's discovered settings, in key order — rows the catalog knows nothing about."""
    return [discovered_setting(key) for key, cached in sorted(cache.items()) if cached.discovered]


def _all_specs(cache: dict) -> list[RemoteSetting]:
    """Every setting this node's page draws: the catalog, plus what it taught us itself."""
    return [*REPEATER_SETTINGS, *_discovered_specs(cache)]


def _extra_keys(cache: dict) -> frozenset[str]:
    """The keys on this node's page that came from the node rather than from the catalog."""
    return frozenset(key for key, cached in cache.items() if cached.discovered)


@dataclass(frozen=True, slots=True)
class ReadOne:
    """What the admin menu resolves with when ``^R`` asks for one setting's value again.

    Attributes:
        key: The highlighted setting's catalog key.
    """

    key: str


class AdminMenu(SelectScreen):
    """The admin editor's list, plus a key that re-reads the highlighted setting alone.

    A full read is one paced round trip per setting — minutes, on a node with every
    section — so checking whether one value took, or refreshing the one row that timed
    out, must not cost all of them. The key is ``^R``, the chord the app already spends on
    *retry* (chat re-sends an unacknowledged message with it): here too it asks the mesh
    the same question again, for the one thing under the cursor. It has to be a chord,
    because this list filters as you type and every bare letter is spoken for.

    The footer names it, and the F-key lane lights its ``Read`` chip, only on a readable
    setting row — never on an action row, where there is nothing to read.

    It is a full-screen page, not a floating popup: a node's whole catalog is a place the
    reader works in for a while, like the local Device config editor it mirrors, and a
    content-sized box over the node picker spent the frame's width on a backdrop. The
    picker, the value prompts, the confirms and the progress dialogs still float over it.
    """

    floating = False

    #: The keys on this node's page that the catalog hasn't got, refreshed with the rows.
    extra_keys: frozenset[str] = frozenset()

    def __init__(self, title: str, items: list, **kwargs: Any) -> None:
        """Build the page's list, its rows ending at the edge rather than sliding under ←→.

        The Device config page's handling, kept here on purpose. The Actions rows pin a head
        block (:func:`~meshterm.ui.menus.menu_rows`), which on its own turns ←→ scrolling on
        for the whole list — and a setting row, pinning nothing, then slid its label out of
        view along with its description, under a footer that grew a ``←→ scroll`` atom.
        ``hscroll=False`` keeps every row whole and cut with the ellipsis, as every other
        editor lane is.
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
        """The highlighted row's setting key, when it is a setting the node can be asked."""
        key = self._row_key()
        if key is None:
            return None
        if key in self.extra_keys:
            return key  # a discovered row is a string setting, and always re-readable
        spec = get_setting(key)
        return spec.key if spec is not None and spec.readable else None

    def _forgettable_key(self) -> str | None:
        """The highlighted row's key, when it is a discovered row Del can drop."""
        key = self._row_key()
        return key if key is not None and key in self.extra_keys else None

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The list's own hint, plus the read atom while a readable setting is highlighted."""
        base = super().footer_hint
        return splice_hint(base, READ_ONE_HINT) if self._readable_key() else base

    @property
    def fkey_lane(self):
        """The list's lane with ``Read`` on F3 and ``Forget`` behind it — dim where inert.

        F1/F2 are this grouped list's section jumps and F4/F5 the pager; F3 is the slot a
        select list spends on its own verb. Read and Forget are the same slot's two halves
        because they are the same subject — *this row* — and they are never both live: a
        catalog row can be read and not forgotten, and only a discovered row can be dropped.
        Dim rather than empty on an action row: both are things on this screen, just not for
        the row the cursor is on.
        """
        from .tui.fkeys import FPair

        lane = list(super().fkey_lane)
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
        """Ask for the highlighted setting's value, or behave as any select list does."""
        if action == "retry":
            key = self._readable_key()
            if key is not None:
                self.resolve(ReadOne(key))
            return
        super().handle(action, data)


async def open_repeater_admin(ctx: AppContext) -> dict[str, Any] | None:
    """Run the repeater-admin flow: pick a node, log in, and administer it.

    The pick is a popup — a question on the way in, gone once answered — so the admin page
    opens over the main menu and Esc from it lands there: the page is the whole visit. A
    login the node refuses (or never answers) re-asks the question with the same node
    highlighted, so a mistyped password is one Enter and a retry, not a round trip. The
    list used to stay pushed under the page as a hub, which read as two places where
    there is one.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).

    Returns:
        A summary of the node administered (for the tool's log), or ``None`` if the reader
        left the picker without ever getting into a session.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .admin_picker import pick_admin_node
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("repeater admin is only available in the menu")

    device = await ctx.device()
    # Through the session cache — opening this screen shouldn't re-read the (slow) contacts
    # table when another screen already has (see
    # :class:`~meshterm.services.device_state.DeviceState`); the device handle below is still
    # needed for the admin login and the CLI session that follow.
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
    """Log in to ``node``: remembered password silently, else one floating prompt.

    A working password is remembered. A *rejected* one is forgotten so the next attempt
    asks fresh (the app-wide remote-admin convention) — but only rejected: a node that
    never answered has said nothing about the password, so silence keeps it. Administering
    a repeater that happened to be down used to erase its credential on the way past.
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
            # The picker popup is gone by now, so this is the only frame: the flag is what
            # keeps it a box over a blank base rather than a full-frame prompt.
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
    """Run the editor loop for one logged-in node.

    One screen for the whole session, its rows refreshed in place after every action: they
    carry the cache's ``current → new`` values, and the title counts what is staged, so the
    content moves under a highlight that stays where the reader put it — typed filter
    included. Every sub-prompt floats over it, and Esc leaves the node.
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
                # This node's page, not the catalog: a discovered row is refreshed by the
                # same sweep as everything else, and reads n/a if it stops answering.
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
                    # A read-only fact has nothing to stage; asking again is all Enter can do.
                    await read_settings(ctx, device, node, [spec])
                else:
                    await _stage_setting(ctx, str(choice), cache, pending)
            # A read or an apply rewrites the cache the rows are drawn from; re-read it so
            # the values are the ones the action just produced.
            cache = ctx.remote_store.settings(node)
            title, items = _menu_items(node, cache, pending)
            menu.extra_keys = _extra_keys(cache)
            menu.replace_items(items, title=title)


# --- the menu ------------------------------------------------------------------------


def _menu_items(node: Contact, cache: dict, pending: dict[str, str]) -> tuple[str, list]:
    """Build the editor menu's title and rows for the cache + staged state.

    The same lane layout as the local device-configuration editor — setting, value
    (with any staged ``→ new``), description under one header line — so administering
    a remote node reads exactly like configuring the local one.
    """
    sections: list[tuple[str, list[tuple[str, Text, str, Any]]]] = []
    for category, specs in settings_by_category():
        rows: list[tuple[str, Text, str, Any]] = []
        for spec in specs:
            if spec.key == "lat":
                # The map pick sets both coordinates at once, so it heads the pair it fills —
                # the same row, in the same place, as on the Device config page.
                rows.append((PICK_LOCATION_LABEL, Text(), PICK_LOCATION_HELP, _LOCATION))
            rows.append((spec.label, _value_text(spec, cache, pending), spec.help, spec.key))
        sections.append((category, rows))

    # What this node taught us itself, last and in its own section — absent entirely on a
    # node that has taught us nothing, which is every node until someone asks it something.
    # Its values are fitted to the lane the catalog's rows already need: the value lane is
    # one width for the whole page, so an unbounded reply here would push every setting's
    # description right, off the edge of a 72-column screen, and reshape a page the reader
    # opened to read the catalog.
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

    # The same pinned, self-fitting header the local editor heads its lanes with: it stays
    # on screen under the category headings for the whole list, and abbreviates instead of
    # wrapping when a long cached value leaves the last label no room (menus.lane_header).
    items: list = [Separator(lambda w: lane_header(label_w, value_w, w), pinned=True)]
    for category, rows in sections:
        items.append(section_heading(category))
        for label, value, help_text, key in rows:
            items.append(
                Choice(
                    title=lane_row(label, value, help_text, label_w, value_w),
                    value=key,
                    # Only a discovered row can be dropped: a catalog row that goes away
                    # would come straight back on the next paint, the catalog still naming it.
                    deletable=category == DISCOVERED_CATEGORY,
                )
            )

    # ↻ and ⌨ are one cell where 📡 🕒 🔐 🔄 are two, so the column is measured once and
    # every mark padded out to it — otherwise Read settings and Command line start their
    # labels a column left of the rows under them.
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
    """One setting's VALUE lane: what the node last said, and any staged arrow.

    Four states, each its own word: the value; ``empty`` for a string the node holds blank;
    ``n/a`` when the node answered with something that can't be this setting's value — its
    firmware has no such setting; ``?`` when it was never read (or never answered). No age:
    the lane is the value, and a stamp beside it only crowded it.
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


async def _stage_setting(ctx: AppContext, key: str, cache: dict, pending: dict[str, str]) -> None:
    """Prompt for one setting's new value and stage it (nothing is sent yet)."""
    spec = _spec_for(key, cache)
    if spec is None or not spec.writable:  # pragma: no cover - menu offers only real keys
        return
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
        pending.pop(key, None)  # back to what the node last said — nothing to send
    else:
        pending[key] = value


async def _forget_discovered(
    ctx: AppContext, node: Contact, key: str, pending: dict[str, str]
) -> None:
    """Drop one discovered row from this node's page, behind a confirm.

    This is the *only* way a discovered row is removed. Letting a read drop one — a key the
    node no longer answers is real enough, after a board swap onto a reused identity — would
    mean a single misread reply silently deleting the row: a truncated line, a node answering
    mid-reboot, a stray frame correlated to the wrong command. A catalog row costs an ``n/a``
    when that happens and nothing is lost, because the catalog still names the key; a
    discovered row's key is known only here, and the reader may not remember what it was. So
    the same ``n/a`` is all a read may do, and removing is a decision.

    It is a red confirm like any other single-record delete, though nothing on the node
    changes: what goes is what MeshTerm remembers, and asking the key again brings it back.
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
    """Pick the node's advertised location on the map and stage both coordinates it sets.

    The Device config page's row, speaking this node's CLI: the map opens on the position
    as staged (or as the node last said), and on the mesh where there is none. Each picked
    coordinate is stored the way a read of it would be, so picking the spot the node
    already holds unstages rather than queueing a no-op ``set``. Nothing is sent until Apply.
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
        # Six decimals ≈ 0.1 m — beyond the map's own precision, plenty for an advert.
        text = normalize_value(spec, f"{value:.6f}")
        if text == known(key):
            pending.pop(key, None)  # what the node already says — nothing to send
        else:
            pending[key] = text


def _known_values(ctx: AppContext, node: Contact) -> dict[str, str]:
    """The node's last-read values, for restating a composite's unstaged fields."""
    return {
        key: cached.value
        for key, cached in ctx.remote_store.settings(node).items()
        if cached.supported
    }


def _remark(reply: str, value: str) -> str:
    """What a successful write's reply adds beyond ``OK`` (``reboot to apply``), or ``""``."""
    remark = re.sub(r"^ok\b[\s,:-]*", "", reply.strip(), flags=re.IGNORECASE)
    return "" if remark.lower() in ("", value.lower()) else remark


async def _apply(ctx: AppContext, device: Device, node: Contact, pending: dict[str, str]) -> int:
    """Send every staged value, paced, under an abortable progress dialog.

    Staged values go out as :func:`~meshterm.core.remote_config.write_plan` groups them:
    one command per setting, except the radio's four fields, which travel as one
    ``set radio``. That command restates every field, so a radio field staged while its
    siblings were never read first reads them — one paced ``get radio`` — rather than
    guessing a frequency. Each confirmed value folds straight into the per-node cache (and
    stales any other spelling of the same firmware value); a rejected or unanswered write
    stays staged so it can be retried (or unstaged) rather than being silently dropped.
    Records one ``runs`` row for the batch.

    Returns:
        How many settings the node accepted.
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
            if not first:  # every transmission after the first waits out the cooldown
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
        # The catalog plus this node's own discovered rows: a staged key that only this node
        # has is still a row with a label to name in the outcome, and a write to count.
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
            verb = " ".join(write.command.split()[:2])  # never a secret's value on screen
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
    """Fold one read's reply into the cache for every setting it answers.

    A reply that can't be the setting's value is the node saying it has no such setting,
    and is remembered as that (the row reads ``n/a``) — an error (``??: key``, ``Error:
    unsupported``) and an answer to some other question alike. The second is how older
    firmware says it: ``get`` matches its keys by *prefix*, so on v1.15 a key it lacks
    falls into a shorter sibling's branch — ``get radio.fem.rxgain`` answers with
    ``get radio``'s ``> 910.525,62.5,7,5`` — and ``gps`` on a board whose receiver is
    absent answers ``Can't find GPS``. Leaving those rows alone left them on ``?``, which
    says *never asked*; only a read that got no reply at all (not passed here) keeps that.

    Returns:
        How many settings got a value.
    """
    got = 0
    for spec in fills:
        value = parse_reply_value(spec, reply)  # None for an error reply, too
        if value is None:
            ctx.remote_store.remember_unsupported(node, spec.key)
        else:
            ctx.remote_store.remember_setting(node, spec.key, value)
            got += 1
    return got


def learn_from_cli(ctx: AppContext, node: Contact, command: str, reply: str) -> None:
    """Fold what one command-line exchange proved about ``node`` into its page.

    The command line is the only place a setting outside the catalog can be reached, so it
    is also the only place one can be *found*: a ``get``/``set`` this node answered is proof
    the key is there, and the row it earns costs no round trip nobody asked for. The catalog
    doesn't grow — a key learned here belongs to this node alone (see
    :func:`~meshterm.core.remote_config.discovered_setting`), and every other node's page is
    what it always was.

    Three cases, in the order they are tested:

    * A **composite's own key** (``get radio``) fills all four of its fields, exactly as a
      read of any one of them does.
    * A **catalog key** folds into the cache the way the sweep or an Apply would — which is
      also how ``get tx`` at the command line stopped leaving the TX power row stale.
    * Anything else is **discovered**, and the two verbs are not equally good evidence.
      ``handleSetCmd`` matches a key with its trailing space and refuses an unknown one
      outright, so an accepted write is proof. ``handleGetCmd`` matches on a bare prefix and
      will answer a key it hasn't got out of a shorter key's branch, so a read is proof only
      when the reply cannot be that shorter key's
      (:func:`~meshterm.core.remote_config.learnable_from_get`).

    ``prv.key`` is never stored, whichever way it was asked
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
        return  # a refused write says nothing about the key, and nothing about its value
    if members:
        for member in members:
            value = parse_reply_value(member, parsed.value)
            if value is not None:
                ctx.remote_store.remember_setting(node, member.key, value)
        return
    if spec is not None and not spec.discovered:
        value = parse_reply_value(spec, parsed.value)
        if value is None:
            return  # sent in words the catalog can't read back; leave the row as it was
        ctx.remote_store.remember_setting(node, spec.key, value)
        for other in spec.overlaps:
            ctx.remote_store.forget_setting(node, other)
        return
    ctx.remote_store.remember_discovered(node, parsed.key, parsed.value)


async def read_settings(
    ctx: AppContext, device: Device, node: Contact, specs: Iterable[RemoteSetting]
) -> None:
    """Refresh ``specs`` from the node, one paced read at a time.

    The four radio fields share one ``get radio`` (see
    :func:`~meshterm.core.remote_config.read_plan`), so a full read asks each question once.
    Values that parse land in the cache (and on screen), a setting the firmware lacks reads
    ``n/a``, and abort keeps everything already read. Reads the node never answered are
    listed afterwards, because their rows go on showing what they last said and would
    otherwise pass for fresh.
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
        title: The dialog's heading.
        work: ``async work(dialog)`` performing the batch, updating ``dialog.status``.

    Returns:
        ``True`` if the user aborted, ``False`` if the batch ran to completion.
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
    except Exception as exc:  # noqa: BLE001 - surface in a dialog, keep the screen
        await session.message_dialog(Text(str(exc), style="err"), title=title)
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - a spinner hiccup must never break the batch
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
    """Confirm and send one fixed CLI command, showing the node's reply."""
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
    """Change the node's admin password (and re-remember it on success)."""
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
        ctx.admin_store.remember(node, new)  # the working password just changed
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
    """Open the readline-style remote CLI for ``node`` (history persists per node)."""
    from .remote_cli import RemoteCliScreen

    session = ctx.ui.session
    worker: asyncio.Task | None = None
    ticker: asyncio.Task | None = None

    def send(command: str) -> None:
        nonlocal worker
        if worker is not None and not worker.done():
            return  # one command in flight at a time — every send is a transmission
        screen.sent(command)
        ctx.remote_store.append_history(node, command)
        worker = asyncio.ensure_future(roundtrip(command))

    async def roundtrip(command: str) -> None:
        try:
            reply = await device.send_remote_command(node, command, timeout=_REPLY_TIMEOUT_S)
        except asyncio.CancelledError:
            screen.failed("cancelled", error=False)
            raise
        except Exception as exc:  # noqa: BLE001 - shown inline, the screen stays up
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
                except Exception:  # noqa: BLE001 - the screen is closed; nothing to surface
                    pass
