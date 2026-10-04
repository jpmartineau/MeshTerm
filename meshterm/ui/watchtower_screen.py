# SPDX-License-Identifier: Apache-2.0
"""The Watchtower screens: the alert log, the watchlist, and per-node rules.

The interactive face of the ``watchtower`` tool. One select-list screen carries the
whole feature (the persistent-backdrop pattern the config editors use): the alert log
newest-first — unacknowledged alerts lead with the header badge's red marker, Enter
acknowledges one — followed by the watchlist (Enter opens a node's rule popover) and
the actions: star another node, toggle the mesh-wide new-node rule, acknowledge or
clear in bulk.

Everything here reads and writes the :class:`~meshterm.core.watch_store.WatchStore`;
the rules themselves run in :mod:`meshterm.services.watchtower`, which the menu starts
with the other always-on services (and this screen nudges, idempotently, in case it
is opened before that ever happened). Nothing transmits.
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

#: Alert-kind display styles: alarms red, warnings amber, notes calm.
_KIND_STYLES = {
    "silence": "err",
    "snr": "warn",
    "new-node": "brand",
    "recovered": "ok",
    "courier": "accent",  # outbox outcomes (see services.courier) share the log
}

#: How many alerts the screen lists (the store keeps more; the tail rarely matters).
_SHOWN_ALERTS = 40

# Menu action sentinels (tuples so they never collide with alert ids or node keys).
_WATCH = ("watch",)
_TOGGLE_NEW = ("toggle-new",)
_ACK_ALL = ("ack-all",)
_CLEAR = ("clear",)


def contact_watch_key(contact: Contact) -> str | None:
    """The watch-store key for a contact: the 12-hex id observations carry."""
    ident = (contact.public_key or contact.key_prefix or "").lower().removeprefix("0x")
    return ident[:12] or None


async def open_watchtower(ctx: AppContext) -> dict[str, Any] | None:
    """Run the Watchtower screen until dismissed.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Returns:
        A summary of what happened (for the tool's log), or ``None`` on plain exit.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi
    from .tui import CANCEL, SelectScreen

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the watchtower is only available in the menu")
    session = ctx.ui.session
    await ctx.watchtower.start()  # idempotent; normally already running

    contacts: list[Contact] = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - the log and rules render fine without contacts
        contacts = []

    store = ctx.watch_store
    # Legacy watched entries (starred before types were stored) fall back to the
    # contact table's advertised type; alert labels resolve back to keys for their hue.
    type_by_key = {
        key: c.node_type
        for c in contacts
        if (key := contact_watch_key(c)) is not None and c.node_type is not None
    }
    contact_key_of = make_name_key_resolver(contacts)

    def rows() -> list:
        """The current alert log and watch list, as menu rows.

        An alert's node type comes by its label — the contact table's advertised type wins
        (freshest), a watched entry's stored type backfills — so an alert's name gets its
        glyph even for a node no longer in the device's contacts.
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

    # One screen for the whole visit, its rows refreshed in place after each action — every
    # one of them changes the list it was chosen from (an ack rewrites its row, a star adds
    # one, Clear takes several away), and the highlight rides along to wherever its row went.
    menu = SelectScreen(
        "Watchtower — alerts & watched nodes",
        rows(),
        hscroll=True,
        # ←→ scroll is surfaced by the list itself, but only while the highlighted
        # alert actually overflows the width (see SelectScreen.hscroll_hint) — and it
        # slides the *message* alone: each row pins its own lanes (see _alert_lanes).
        # The two section headings below earn the list its ^PgUp/^PgDn jumps and their
        # F1/F2 chips for free (SelectScreen.picocalc_lyra_lane); the hint has no room to name
        # them beside the ←→ atom, and no other grouped list spells them out either.
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
    """Build the screen's rows: alerts, then the watchlist, then the actions.

    Args:
        alerts: The alert log, newest first.
        watched: The watchlist by canonical id.
        new_node_alerts: Whether the mesh-wide new-node rule is on.
        type_of: Maps a watch key to a node type, for entries starred before types
            were stored.
        key_of: Maps an alert's node label back to a key, for its hue; layered here
            with the watchlist's own names so a starred node's alerts colour even
            when the device (and its contact table) is offline.
        alert_type_of: Maps an alert's node label to that node's type, for the type
            glyph that leads its name (the plain-node ``●`` when unknown).
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
    # One measured icon column for every action row on this screen, across its headings: the
    # terminal draws ✓ and 🗑 in one cell and ⭐ and 🔔 in two, so the bulk actions used to
    # start their words a column left of Watch a node… and the new-node toggle below them.
    # Declared whole rather than from the rows present, so the column holds still as the
    # conditional rows come and go; empty where the platform draws no icons.
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
    """An alert's fixed head: marker, age, kind, type glyph, node — and the ``—`` lead-in.

    The leading ``●``/``○`` is the *acknowledgement* state, so the node's own type marker
    (``▲`` repeater, ``●`` node, …) leads the node name instead — the map's shared marker
    palette in its own type colour, resolved from the node's key through ``type_of`` (the
    plain-node ``●`` when the type is unknown, matching the watchlist rows). An unacked
    alert's node label takes its key-derived hue (resolved through ``key_of``, muted when
    no key is known); an acked row recedes to muted throughout — the type glyph included —
    with the rest of its history.

    Split from the message so the row can measure what it pins out of the ←→ scroll (see
    :attr:`~meshterm.ui.tui.select.Choice.hscroll_from`): these lanes are *which alert this
    is*, and reading a long message to its end is no reason to lose the row's identity.
    The ``—`` stays with the head so the message still arrives introduced, whatever it is
    slid to.
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
    """One alert as a row: its fixed lanes, then the message ``←→`` scrolls."""
    row = lanes.copy()
    row.append(alert.message, style="muted")
    return row


def _watched_row(entry: WatchedNode, type_of: Callable[[str], int | None]) -> Text:
    """One watched node as a row: type glyph, hued name, its rules, and last heard.

    The leading glyph is the node's shared type marker (``▲`` repeater, ``●`` node, …)
    in its own type colour — silence is signalled by the trailing ``⚠ silent``, not the
    glyph — and the name takes its key-derived hue (the entry's 12-hex watch key).
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
    """Float the star-a-node picker: unwatched contacts, most recently heard first."""
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
    """Float one watched node's rule editor until dismissed (or the node is unstarred)."""
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


#: Cells between the widest rule name and its value in the rule popover.
_RULE_VALUE_GAP = 6


def _rule_items(entry: WatchedNode) -> list:
    """One watched node's rule popover rows: its two rules with their values, then Stop.

    The terminal draws ``🕒`` and ``📶`` in two cells and ``✗`` in one, so the rows share one
    measured icon column — the Stop row used to start its words a column left of the rules
    above it. The value lane is measured too, in cells from the rule names, rather than
    padded by hand with spaces: it then sits in one column whatever the icon column comes
    to, including nothing at all where the platform draws no icons.

    Args:
        entry: The watched node whose rules the rows show.

    Returns:
        The popover's rows, ``"silence"``/``"snr"``/``"unwatch"`` as their values.
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
    """Float the silence-threshold picker for one watched node."""
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
