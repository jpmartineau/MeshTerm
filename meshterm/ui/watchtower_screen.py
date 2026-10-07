# SPDX-License-Identifier: Apache-2.0
"""The Watchtower screens: the alert log, the watchlist, and per-node rules.

This is the interactive face of the ``watchtower`` tool. One select-list screen has the
whole feature (the pattern of the persistent backdrop that the config editors use). First
is the alert log, with the newest alert first. An alert that nobody acknowledged starts
with the red marker of the header badge, and Enter acknowledges one alert. After the log is
the watchlist (Enter opens the rule dialog of a node). Last are the actions: star another
node, toggle the rule for new nodes on the whole mesh, and acknowledge or clear alerts in
bulk.

All the code here reads and writes the :class:`~meshterm.core.watch_store.WatchStore`. The
rules themselves run in :mod:`meshterm.services.watchtower`. The menu starts that service
with the other services that always run. This screen also starts it, with no effect if it
already runs, in case the user opens the screen before the menu started it. Nothing
transmits.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich.cells import cell_len
from rich.text import Text

from ..core.models import Contact
from ..core.watch_store import OFF, SILENCE_CHOICES_H, Alert, WatchedNode
from ..services.trace_runner import make_name_key_resolver
from .menus import icon_lane, icon_mark, marked_label, section_heading
from .theme import name_style
from .tui import Choice, Separator
from .widgets import DEFAULT_GLYPH, NODE_GLYPHS, age_seconds, format_age

if TYPE_CHECKING:
    from ..context import AppContext

#: The display styles for the kinds of alert: alarms are red, warnings are amber, and notes
#: are calm.
_KIND_STYLES = {
    "silence": "err",
    "snr": "warn",
    "new-node": "brand",
    "recovered": "ok",
    "courier": "accent",  # the results of the outbox (refer to services.courier) share the log
}

#: The number of alerts that the screen lists. The store keeps more, but the older ones
#: rarely matter.
_SHOWN_ALERTS = 40

# The sentinels of the menu actions. They are tuples, so that they never collide with
# alert ids or node keys.
_WATCH = ("watch",)
_TOGGLE_NEW = ("toggle-new",)
_ACK_ALL = ("ack-all",)
_CLEAR = ("clear",)


def contact_watch_key(contact: Contact) -> str | None:
    """The key of a contact in the watch store: the id of 12 hex digits that observations carry."""
    ident = (contact.public_key or contact.key_prefix or "").lower().removeprefix("0x")
    return ident[:12] or None


async def open_watchtower(ctx: AppContext) -> dict[str, Any] | None:
    """Run the Watchtower screen until the user dismisses it.

    Args:
        ctx: The shared application context (it must run the interactive TUI).

    Returns:
        A summary of what happened (for the log of the tool), or ``None`` for a plain exit.

    Raises:
        RuntimeError: If the caller is outside the interactive menu (no full-screen
            session).
    """
    from .surface import TuiUi
    from .tui import CANCEL, SelectScreen

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the caller is for the menu only
        raise RuntimeError("the watchtower is only available in the menu")
    session = ctx.ui.session
    await ctx.watchtower.start()  # no effect if it runs already, and it normally does

    contacts: list[Contact] = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - the log and the rules render correctly with no contacts
        contacts = []

    store = ctx.watch_store
    # An old watched entry (starred before MeshTerm stored types) uses the advertised type
    # from the contact table. The labels of alerts resolve back to keys, for their hue.
    type_by_key = {
        key: c.node_type
        for c in contacts
        if (key := contact_watch_key(c)) is not None and c.node_type is not None
    }
    contact_key_of = make_name_key_resolver(contacts)

    def rows() -> list:
        """The current alert log and watchlist, as menu rows.

        The node type of an alert comes from its label. The advertised type in the contact
        table has priority, because it is the newest. The stored type of a watched entry
        fills the gaps. Thus the name of an alert gets its glyph also for a node that is no
        longer in the contacts of the device.
        """
        type_by_name: dict[str, int] = {
            entry.name.casefold(): entry.node_type
            for entry in store.watched().values()
            if entry.node_type is not None
        }
        type_by_name.update(
            {c.name.casefold(): c.node_type for c in contacts if c.node_type is not None}
        )
        return _menu_items(
            store.alerts(),
            store.watched(),
            store.new_node_alerts,
            type_of=type_by_key.get,
            key_of=contact_key_of,
            alert_type_of=lambda label: type_by_name.get(label.casefold()),
        )

    # One screen for the whole visit. MeshTerm refreshes its rows in place after each
    # action. Each action changes the list from which the user selected it: an ack changes
    # its row, a star adds a row, and Clear removes several rows. The highlight goes with
    # its row to the new place.
    menu = SelectScreen(
        "Watchtower — alerts & watched nodes",
        rows(),
        hscroll=True,
        # The list itself shows the ←→ scroll, but only while the highlighted alert is
        # wider than the screen (refer to SelectScreen.hscroll_hint). It slides only the
        # *message*, because each row pins its own lanes (refer to _alert_lanes). The two
        # section headings below give the list its ^PgUp/^PgDn jumps and their F1/F2 chips
        # with no extra code (SelectScreen.picocalc_lyra_lane). The hint has no room to name
        # them beside the ←→ atom, and no other grouped list names them either.
        footer_hint="↑↓ move · Enter select/acknowledge · Esc back",
    )
    async with session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            if choice in (None, CANCEL):
                return {"watched": len(store.watched()), "unacked": store.unacked_count()}
            if choice == _WATCH:
                await _pick_node(ctx, contacts)
            elif choice == _TOGGLE_NEW:
                store.set_new_node_alerts(not store.new_node_alerts)
            elif choice == _ACK_ALL:
                store.ack_all()
            elif choice == _CLEAR:
                store.clear_acked()
            elif isinstance(choice, tuple) and choice[0] == "ack":
                store.ack(int(choice[1]))
            elif isinstance(choice, tuple) and choice[0] == "node":
                await _node_rules(ctx, str(choice[1]))
            menu.replace_items(rows())


# --- the menu ---------------------------------------------------------------------------


def _menu_items(
    alerts: list[Alert],
    watched: dict[str, WatchedNode],
    new_node_alerts: bool,
    *,
    type_of: Callable[[str], int | None] = lambda key: None,
    key_of: Callable[[str], str | None] = lambda name: None,
    alert_type_of: Callable[[str], int | None] = lambda label: None,
) -> list:
    """Build the rows of the screen: alerts, then the watchlist, then the actions.

    Args:
        alerts: The alert log, newest first.
        watched: The watchlist, by canonical id.
        new_node_alerts: Whether the rule for new nodes on the whole mesh is on.
        type_of: Gives a node type for a watch key, for entries that were starred before
            MeshTerm stored types.
        key_of: Gives a key for the node label of an alert, for its hue. The function adds
            the names of the watchlist to this. Thus the alerts of a starred node have a
            colour also when the device (and its contact table) is offline.
        alert_type_of: Gives the type of the node for the node label of an alert, for the
            type glyph before its name (the plain-node ``●`` when the type is unknown).
    """
    watched_keys = {entry.name.casefold(): entry.key for entry in watched.values()}

    def label_key(label: str) -> str | None:
        return key_of(label) or watched_keys.get(label.casefold())

    items: list = [section_heading("Alerts")]
    if not alerts:
        items.append(Separator("  nothing yet — tripped rules land here"))
    for alert in alerts[:_SHOWN_ALERTS]:
        lanes = _alert_lanes(alert, label_key, alert_type_of)
        items.append(
            Choice(
                _alert_row(lanes, alert),
                ("ack", alert.ident),
                hscroll_from=lanes.cell_len,
            )
        )
    # One measured icon column for each action row on this screen, across its headings. The
    # terminal draws ✓ and 🗑 in one cell, and ⭐ and 🔔 in two cells. Thus the bulk
    # actions once started their words one column to the left of Watch a node… and of the
    # toggle for new nodes below them. The set of icons is declared whole and not found from
    # the rows that are present. Thus the column does not move when the conditional rows
    # appear and disappear. It is empty where the platform draws no icons.
    lane = icon_lane(("✓", "🗑", "⭐", "🔔"))
    unacked = sum(1 for a in alerts if not a.acked)
    acked = len(alerts) - unacked
    if unacked:
        ack_all = marked_label("✓", f"Acknowledge all ({unacked})", "ok", lane=lane)
        items.append(Choice(ack_all, _ACK_ALL))
    if acked:
        items.append(Choice(marked_label("🗑", "Clear acknowledged", "err", lane=lane), _CLEAR))

    items.append(Separator(" "))
    items.append(section_heading("Watched nodes"))
    if not watched:
        items.append(Separator("  none starred yet — silence and SNR rules need one"))
    for key in sorted(watched, key=lambda k: watched[k].name.casefold()):
        items.append(Choice(_watched_row(watched[key], type_of), ("node", key)))
    items.append(Choice(marked_label("⭐", "Watch a node…", "", lane=lane), _WATCH))

    items.append(Separator(" "))
    state = "[ok]on[/ok]" if new_node_alerts else "[muted]off[/muted]"
    toggle = Text.from_markup(
        f"New-node alerts: {state}  [muted]— announce first-ever appearances[/muted]"
    )
    items.append(Choice(Text.assemble(icon_mark("🔔", "", lane), toggle), _TOGGLE_NEW))

    return items


def _alert_lanes(
    alert: Alert,
    key_of: Callable[[str], str | None],
    type_of: Callable[[str], int | None] = lambda label: None,
) -> Text:
    """The fixed head of an alert: marker, age, kind, type glyph, node, and the ``—`` lead-in.

    The first ``●`` or ``○`` is the *acknowledgement* state. Thus the type marker of the
    node itself (``▲`` repeater, ``●`` node, and other markers) is before the node name
    instead. It is the marker from the shared palette of the map, in its own type colour.
    The function resolves it from the key of the node through ``type_of``. The plain-node
    ``●`` is for an unknown type, the same as in the watchlist rows. The node label of an
    alert that nobody acknowledged has the hue that comes from its key (resolved through
    ``key_of``, and muted when no key is known). An acknowledged row is muted in all its
    parts, with the type glyph, the same as the rest of its history.

    The head is separate from the message. Thus the row can measure what it pins out of the
    ←→ scroll (refer to :attr:`~meshterm.ui.tui.select.Choice.hscroll_from`). These lanes
    show *which alert this is*, and the user who reads a long message to its end must not
    lose the identity of the row. The ``—`` stays with the head, so the message always has
    its lead-in, at each scroll position.
    """
    row = Text()
    if alert.acked:
        row.append("○ ", style="muted")
    else:
        row.append("● ", style="err")
    age = format_age(age_seconds(alert.when))
    row.append(f"{age:>5}  ", style="muted")
    row.append(alert.kind.ljust(10), style=_KIND_STYLES.get(alert.kind, "brand"))
    glyph, glyph_style = NODE_GLYPHS.get(type_of(alert.label), DEFAULT_GLYPH)
    row.append(f"{glyph} ", style="muted" if alert.acked else glyph_style)
    if alert.acked:
        row.append(alert.label, style="muted")
    else:
        row.append(alert.label, style=name_style(alert.label, key_of(alert.label)))
    row.append(" — ", style="muted")
    return row


def _alert_row(lanes: Text, alert: Alert) -> Text:
    """One alert as a row: its fixed lanes, then the message that ``←→`` scrolls."""
    row = lanes.copy()
    row.append(alert.message, style="muted")
    return row


def _watched_row(entry: WatchedNode, type_of: Callable[[str], int | None]) -> Text:
    """One watched node as a row: type glyph, name with its hue, its rules, and last heard.

    The first glyph is the shared type marker of the node (``▲`` repeater, ``●`` node, and
    other markers) in its own type colour. The ``⚠ silent`` at the end signals silence, not
    the glyph. The name has the hue that comes from its key (the watch key of the entry, 12
    hex digits).
    """
    node_type = entry.node_type if entry.node_type is not None else type_of(entry.key)
    glyph, glyph_style = NODE_GLYPHS.get(node_type, DEFAULT_GLYPH)
    row = Text()
    row.append(f"{glyph} ", style=glyph_style)
    row.append(entry.name, style=name_style(entry.name, entry.key))
    silence = "off" if entry.silence_hours == OFF else f"{entry.silence_hours} h"
    row.append(f"   silence {silence}", style="muted")
    row.append(" · SNR watch " + ("on" if entry.snr_watch else "off"), style="muted")
    if entry.silent_since is not None:
        row.append("  ·  ⚠ silent", style="err")
    elif entry.last_heard is not None:
        row.append(f"  ·  heard {format_age(age_seconds(entry.last_heard))}", style="muted")
    return row


# --- the flows --------------------------------------------------------------------------


async def _pick_node(ctx: AppContext, contacts: list[Contact]) -> None:
    """Float the dialog to star a node: contacts that are not watched, most recently heard first."""
    session = ctx.ui.session
    store = ctx.watch_store
    candidates = [
        (key, c)
        for c in contacts
        if (key := contact_watch_key(c)) is not None and not store.is_watched(key)
    ]
    if not candidates:
        message = (
            "Every known contact is already watched."
            if contacts
            else "No contacts available — connect a device that knows some nodes first."
        )
        await session.message_dialog(Text(message, style="muted"), title="Watch a node")
        return

    def recency(pair: tuple[str, Contact]) -> float:
        seen = age_seconds(pair[1].last_seen)
        return seen if seen is not None else float("inf")

    items: list = []
    for key, contact in sorted(candidates, key=recency):
        glyph, glyph_style = NODE_GLYPHS.get(contact.node_type, DEFAULT_GLYPH)
        row = Text()
        row.append(f"{glyph} ", style=glyph_style)
        row.append(
            contact.name,
            style=name_style(contact.name, contact.public_key or contact.key_prefix),
        )
        row.append(f"   heard {format_age(age_seconds(contact.last_seen))}", style="muted")
        items.append(Choice(row, (key, contact)))
    picked = await session.select(
        "Watch a node",
        items,
        prompt="Silence and SNR rules will watch it from now on:",
    )
    if picked is None:
        return
    key, contact = picked
    store.watch(key, contact.name, last_seen=contact.last_seen, node_type=contact.node_type)


async def _node_rules(ctx: AppContext, key: str) -> None:
    """Float the rule editor of one watched node until the user dismisses it or removes the star."""
    session = ctx.ui.session
    store = ctx.watch_store
    while True:
        entry = store.watched().get(key)
        if entry is None:
            return
        picked = await session.select(
            f"Rules — {entry.name}",
            _rule_items(entry),
            filterable=False,
            footer_hint="↑↓ move · Enter change · Esc back",
        )
        if picked is None:  # Esc
            return
        if picked == "silence":
            await _pick_silence(ctx, key, entry)
        elif picked == "snr":
            store.set_snr_watch(key, not entry.snr_watch)
        elif picked == "unwatch":
            store.unwatch(key)
            return


#: The number of cells between the widest rule name and its value in the rule dialog.
_RULE_VALUE_GAP = 6


def _rule_items(entry: WatchedNode) -> list:
    """The rows of the rule dialog of one watched node: its two rules with their values, then Stop.

    The terminal draws ``🕒`` and ``📶`` in two cells and ``✗`` in one cell. Thus the rows
    share one measured icon column. The Stop row once started its words one column to the
    left of the rules above it. MeshTerm also measures the value lane, in cells from the
    rule names, and does not pad it by hand with spaces. Thus it is in one column for each
    width of the icon column, also when the width is nothing, where the platform draws no
    icons.

    Args:
        entry: The watched node whose rules the rows show.

    Returns:
        The rows of the dialog, with ``"silence"``, ``"snr"``, and ``"unwatch"`` as their
        values.
    """
    lane = icon_lane(("🕒", "📶", "✗"))
    silence = "off" if entry.silence_hours == OFF else f"after {entry.silence_hours} h"
    snr = "on" if entry.snr_watch else "off"
    rules = (("🕒", "Silence alarm", silence, "silence"), ("📶", "SNR watch", snr, "snr"))
    name_w = max(cell_len(name) for _, name, _, _ in rules)
    items: list = [
        Choice(
            Text.assemble(
                icon_mark(icon, "", lane),
                name,
                " " * (name_w - cell_len(name) + _RULE_VALUE_GAP),
                value,
            ),
            choice,
        )
        for icon, name, value, choice in rules
    ]
    items.append(Separator(" "))
    items.append(Choice(marked_label("✗", "Stop watching this node", "err", lane=lane), "unwatch"))
    return items


async def _pick_silence(ctx: AppContext, key: str, entry: WatchedNode) -> None:
    """Float the dialog to select the silence threshold for one watched node."""
    session = ctx.ui.session
    items = [Choice("Off — never alarm on silence", OFF)]
    for hours in SILENCE_CHOICES_H:
        label = f"After {hours} h of silence"
        if hours == entry.silence_hours:
            label += "   (current)"
        items.append(Choice(label, hours))
    picked = await session.select(
        f"Silence alarm — {entry.name}",
        items,
        default=entry.silence_hours,
        filterable=False,
        footer_hint="↑↓ move · Enter set · Esc keep",
    )
    if picked is not None:
        ctx.watch_store.set_silence(key, int(picked))
