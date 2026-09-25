# SPDX-License-Identifier: Apache-2.0
"""The Device config page: every setting of the companion, and the operations on the box.

Behind the ``config`` tool (:func:`edit_config`), and shaped deliberately like the
repeater-admin page (:mod:`meshterm.ui.repeater_admin`) — the same full-screen list, the
same setting / value / description lanes under category headings, staged ``current → new``
values, Apply in place, and an **Actions** section closing the list — so configuring the
radio in your hand reads exactly like configuring one over the mesh.

* **Settings** — every value the companion firmware lets an app read and write (see
  :data:`~meshterm.core.device_config.DEVICE_SETTINGS`), each custom variable the device
  reports. Editing a row *stages* the new value and nothing touches the radio until
  *Apply*, which sends the staged values one at a time and stays on the page: one the
  device refuses stays staged with its reason, as on the remote page. Backing out with
  changes staged asks before discarding them.
* **Actions** — the operations on the box itself rather than on a value: re-read, clock
  sync, backup/restore, the identity key, reboot, and factory reset. These *run
  immediately*, each behind its own confirmation (destructive ones behind typing a word),
  and share the snapshot and the :func:`~meshterm.tools.config.apply_ops` executor with
  Apply. The everyday advert lives in its own main-menu entry instead (the ``advert`` tool,
  via :func:`send_advert`), one keystroke away as a popup over the menu.

Multiple-choice values are picked in dialogs (booleans as an On/Off button pair, enums as
a floating select), the node's location can be set by pointing at the full-screen map (see
:class:`~meshterm.ui.map_screen.LocationPickScreen`), and a reboot hands off to the same
reconnect dialog the app shows when a device is unplugged. (Channels have their own
first-class manager — see the ``channels`` tool.)
"""

from __future__ import annotations

import asyncio
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from rich import box
from rich.cells import cell_len
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from ..core.device_config import (
    DEVICE_SETTINGS,
    DeviceConfigError,
    SettingSpec,
    build_snapshot,
    effective_maximum,
    format_value,
    get_spec,
    parse_value,
    settings_by_category,
)
from ..platforms import Platform, on_platform
from .device_info_screen import REVEAL_KEY
from .marks import MASK_MARK
from .menus import (
    PICK_LOCATION_HELP,
    PICK_LOCATION_LABEL,
    confirm_discard,
    exit_rows,
    icon_lane,
    lane_header,
    lane_row,
    marked_label,
    menu_rows,
    run_steps,
    section_heading,
)
from .tui import Choice, SelectScreen, Separator
from .tui.select import splice_hint

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device

# Menu action sentinels (distinct from setting keys, which are plain strings).
_LOCATION = "__location__"
_PRESETS = "__presets__"
_CUSTOM = "__custom__"
#: Prefix of a custom variable's row value; the variable's name follows it.
_CUSTOM_VAR = "__custom_var__:"
_READ = "__read__"
_SYNC_CLOCK = "__sync_clock__"
_REBOOT = "__reboot__"
_BACKUP = "__backup__"
_RESTORE = "__restore__"
_IDENTITY_KEY = "__identity_key__"
_RESET = "__reset__"
_APPLY = "__apply__"
_CANCEL = "__cancel__"
# Sentinel for the "enter a value myself" option on non-strict enum prompts.
_OTHER = "__other__"

#: The setting keys the map pick sets together (each also has its own row, for a typed value).
_COORD_KEYS = ("adv_lat", "adv_lon")

#: How long the reboot flow waits to *observe* the link actually dropping before handing
#: off to the session's reconnect dialog (seconds). A companion normally vanishes from the
#: bus well within this; on timeout we hand off anyway.
_REBOOT_DROP_TIMEOUT_S = 10.0

#: Poll cadence while waiting for the rebooting companion's link to drop (seconds).
_REBOOT_DROP_POLL_S = 0.25


async def cached_snapshot(ctx: AppContext, device: Device) -> dict:
    """A config snapshot for a *screen open*, reusing the devstate session cache.

    The two facts devstate already holds — ``SELF_INFO`` and the path-hash mode — are
    the slowest part of :func:`~meshterm.core.device_config.build_snapshot` to re-ask
    the radio for, and every config apply invalidates them (see
    ``tools/config.py``), so an open can trust the cache. The refresh after a restore,
    key change, or factory reset keeps calling ``build_snapshot(device)`` raw: the
    device state genuinely changed under us there.
    """
    self_info: dict | None = None
    path_hash_mode: int | None = None
    try:
        self_info = await ctx.devstate.self_info()
        path_hash_mode = await ctx.devstate.path_hash_mode()
    except Exception:  # noqa: BLE001 - fall back to the raw reads below
        pass
    from ..tools.config import learn_default_scope

    snapshot = await build_snapshot(device, self_info=self_info, path_hash_mode=path_hash_mode)
    learn_default_scope(ctx, snapshot)
    return snapshot


async def edit_config(ctx: AppContext) -> dict[str, Any] | None:
    """Run the Device config page until the reader leaves it.

    One screen for the whole visit, its rows refreshed in place after every round: they
    carry the staged ``current → new`` values and the title counts them, so the content
    moves under a highlight — and a typed filter — that stay where the reader put them.
    Every sub-prompt floats over the page. Apply sends what is staged and stays (see
    :func:`_apply`); each action runs where it is picked; Esc leaves, asking first while
    anything is still staged; and a reboot closes the page and hands off to the session's
    reconnect dialog.

    Args:
        ctx: Shared application context (provides the connected device and UI surface).

    Returns:
        ``{"applied": n}`` when staged values reached the device during the visit, or
        ``None`` when none did.
    """
    from .tui import CANCEL

    device = await ctx.device()
    snapshot = await cached_snapshot(ctx, device)
    custom = await device.get_custom_vars()

    session = getattr(ctx.ui, "session", None)
    if session is None:  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the config editor is only available in the menu")
    pending: dict[str, Any] = {}  # setting key -> staged new value
    custom_pending: dict[str, str] = {}  # custom variable name -> staged new value
    applied = 0

    def staged() -> int:
        return len(pending) + len(custom_pending)

    def summary() -> dict[str, Any] | None:
        return {"applied": applied} if applied else None

    # The rows read ``snapshot`` and ``custom`` through this closure, so a re-read that
    # rebinds them is exactly what the next refresh draws.
    menu = _ConfigMenu(
        session,
        lambda reveal: _menu_items(
            snapshot,
            pending,
            staged(),
            reveal,
            custom=custom,
            custom_pending=custom_pending,
        ),
        conceals=has_pin(snapshot),
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
    )
    async with session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            if choice is CANCEL:  # Esc at the page
                choice = _CANCEL

            if choice in (None, _CANCEL):
                if staged() and not await confirm_discard(ctx, staged(), verb="applying"):
                    continue  # keep editing — the same page, the same place in it
                return summary()
            if choice == _APPLY:
                applied += await _apply(ctx, device, snapshot, pending, custom_pending)
                snapshot, custom = await _reread(ctx, device)
            elif choice == _READ:
                snapshot, custom = await _reread(ctx, device)
                _unstage_held(snapshot, custom, pending, custom_pending)
            elif choice == _SYNC_CLOCK:
                await _sync_clock(ctx, device, snapshot)
            elif choice == _BACKUP:
                await _backup_now(ctx, device, snapshot)
            elif choice == _RESTORE:
                if await _restore_now(ctx, device, snapshot):
                    snapshot, custom = await _reread(ctx, device)
                    _unstage_held(snapshot, custom, pending, custom_pending)
            elif choice == _IDENTITY_KEY:
                if await _identity_key_menu(ctx, device, snapshot):
                    snapshot, custom = await _reread(ctx, device)
            elif choice == _REBOOT:
                # A reboot ends the visit and would take anything staged with it, so ask
                # first — and a discard agreed to is a discard, whatever the reboot does.
                if staged():
                    if not await confirm_discard(ctx, staged(), verb="applying"):
                        continue
                    pending.clear()
                    custom_pending.clear()
                if await _reboot(ctx, device, snapshot):
                    return summary()  # the link is dropping; the reconnect dialog takes over
            elif choice == _RESET:
                if await _factory_reset(ctx, device, snapshot):
                    # Everything staged was staged against a device that no longer exists.
                    pending.clear()
                    custom_pending.clear()
                    snapshot, custom = await _reread(ctx, device)
            elif choice == _LOCATION:
                await _stage_location(ctx, snapshot, pending)
            elif choice == _PRESETS:
                await _stage_preset(ctx, snapshot, pending)
            elif choice == _CUSTOM:
                await _stage_custom_var(ctx, custom, custom_pending)
            elif isinstance(choice, str) and choice.startswith(_CUSTOM_VAR):
                await _stage_var(ctx, choice.removeprefix(_CUSTOM_VAR), custom, custom_pending)
            else:  # a setting key
                await _stage_setting(ctx, choice, snapshot, pending)
            menu.conceal_pin(has_pin(snapshot))
            menu.refresh()


# --- rendering ---------------------------------------------------------------


def config_table(
    snapshot: dict,
    custom: dict[str, str],
    pending: dict[str, Any] | None = None,
    *,
    reveal_pin: bool = False,
) -> Table:
    """Build the full configuration table, overlaying any staged changes.

    Args:
        snapshot: Device snapshot from ``build_snapshot``.
        custom: Current custom variables.
        pending: Optional staged changes (setting key -> new value).
        reveal_pin: Whether to print the BLE pairing PIN. It is concealed by default —
            this table is the whole-device dump, the thing that gets read over a
            shoulder, screenshotted, and pasted into a bug report, and a pairing code is
            the one value on it that lets someone else's phone onto the radio. The
            Device info page reveals it on a keypress (see
            :class:`~meshterm.ui.device_info_screen.DeviceInfoScreen`); the CLI names it
            one value at a time with ``meshterm config get device_pin``.

    Returns:
        A Rich :class:`Table` of every setting's current (and staged) value plus custom
        variables, ready to hand to ``ctx.ui.show`` / ``ctx.ui.view``.
    """
    pending = pending or {}
    show_staged = bool(pending)
    # The DESCRIPTION lane needs a screen wide enough to hold prose beside the setting and
    # its value(s) — 72 columns, and no third narrow lane. Where it doesn't fit, the table
    # is the settings alone (Rich was already dropping the column outright at those
    # widths) and the explanations stay in the editor, one row at a time.
    describe = _describe and not show_staged
    # Match the contacts list: a frameless SIMPLE_HEAD table with a left-justified accent title
    # and muted headers, so the two screens read as one family (see widgets.contacts_table).
    table = Table(
        title="[accent]Device config[/accent]",
        title_justify="left",
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        header_style="muted",
        expand=False,
        padding=(0, 2, 0, 0),
    )
    # The two narrow lanes are ``no_wrap`` and pre-folded to a cap (see _fold_label /
    # _fold_value), so each takes exactly the room its cap allows and DESCRIPTION — the
    # lane whose content is systematically the widest — keeps the whole remainder. Letting
    # the labels and values size themselves instead gave the descriptions 19 cells of a
    # 72-cell screen and wrapped almost every one of them three lines deep.
    table.add_column("SETTING", style="muted", no_wrap=True)
    table.add_column("CURRENT", no_wrap=True)
    if show_staged:
        table.add_column("STAGED", style="warn", no_wrap=True)
    if describe:
        table.add_column("DESCRIPTION", style="muted")

    def row_of(cells: list[str], description: str) -> list[str]:
        """One table row: the narrow lanes, plus the description where it is drawn."""
        return [*cells, description] if describe else cells

    for category, specs in settings_by_category():
        table.add_section()
        header = [f"[accent]── {category} ──[/accent]", ""]
        if show_staged:
            header.append("")
        table.add_row(*row_of(header, ""))
        for spec in specs:
            current = format_value(spec, spec.getter(snapshot))
            if spec.key == PIN_KEY and not reveal_pin:
                current = conceal(current)
            row = [_fold_label(spec.label, describe), _fold_value(current, describe)]
            if show_staged:
                row.append(
                    _fold_value(format_value(spec, pending[spec.key]), describe)
                    if spec.key in pending
                    else ""
                )
            table.add_row(*row_of(row, spec.help))
    if custom:
        table.add_section()
        blanks = 2 if show_staged else 1
        table.add_row(*row_of(["[accent]── Custom ──[/accent]", *([""] * blanks)], ""))
        for key, value in custom.items():
            row = [_fold_label(key, describe), _fold_value(value, describe)]
            if show_staged:
                row.append("")
            table.add_row(*row_of(row, ""))
    return table


#: The one setting this table holds back: the BLE pairing PIN (the firmware reports it as
#: ``ble_pin``; :data:`~meshterm.core.device_config.SETTINGS` keys it ``device_pin``).
PIN_KEY = "device_pin"


#: How wide a concealed PIN draws: the width of the *field*, taken from the setting's own
#: upper bound (999999), not the length of the value sitting in it. A typed password masks
#: per character because the typist needs to count what they have entered; a stored one
#: shows its field, so the row can't be read for how many digits to guess — and a PIN of
#: ``0`` behind a single bullet would have read as a stray dot rather than a covered value.
_PIN_WIDTH = len(str(get_spec(PIN_KEY).maximum))


def conceal(value: str, *, absent: str = "?") -> str:
    """``value`` as a row of :data:`_PIN_WIDTH` mask bullets.

    A value the device could not report is not a secret, it is an absence: ``format_value``
    already renders that as ``?``, and a ``?`` behind bullets would claim there is
    something to reveal when there is nothing. So only a real value is concealed, and
    :func:`has_pin` asks the same question the other way round — a page with nothing to
    conceal advertises no key for it.

    Args:
        value: The already-rendered value to mask.
        absent: How *this* surface writes "the device never reported one". The menu's
            token is ``?``; the scripted CLI's is :data:`~meshterm.ui.script.NONE`. The
            rule is the same on both and the glyph is not, so the caller names it — a
            ``conceal`` that knew only the menu's token masked the CLI's absence too, and
            six bullets told the reader the radio was PIN-locked when it was not.
    """
    return value if value == absent else MASK_MARK * _PIN_WIDTH


def has_pin(snapshot: dict) -> bool:
    """Whether ``snapshot`` carries a PIN at all — i.e. whether concealing it means anything."""
    return get_spec(PIN_KEY).getter(snapshot) is not None


#: Cell caps for the table's two narrow lanes, indent included. Sized to the widest label
#: and value the settings actually carry, less the few longest — the four that overflow
#: fold onto a second line, which costs nothing on rows whose description was wrapping
#: anyway, and buys every other row ten more cells of prose.
_SETTING_CAP = 24
_VALUE_CAP = 15

#: Each setting's name is indented two spaces so the rows read as sitting *under* their
#: accent section heading, which stays flush-left; a folded continuation hangs two further
#: in, so a wrapped label can never be mistaken for a heading at column 0.
_INDENT = "  "
_HANG = "    "


def _fold_label(label: str, describe: bool) -> str:
    """One SETTING cell: indented, and hard-wrapped at :data:`_SETTING_CAP` if it must be.

    The cap only buys room for the DESCRIPTION lane, so where that lane isn't drawn the
    label keeps its natural width — nothing is competing for the cells.
    """
    if not describe:
        return f"{_INDENT}{label}"
    return (
        "\n".join(
            textwrap.wrap(label, _SETTING_CAP, initial_indent=_INDENT, subsequent_indent=_HANG)
        )
        or _INDENT
    )


def _fold_value(value: str, describe: bool) -> str:
    """One CURRENT/STAGED cell, hard-wrapped at :data:`_VALUE_CAP`.

    An enum value reads ``N (label)``, so the fold prefers the break *before* the
    parenthesised label — ``1`` over ``(2-byte hashes)`` rather than a torn ``1 (2-byte``
    — and a long node name (the field is 31 bytes wide) folds instead of stretching the
    lane across the whole screen.
    """
    if not describe or cell_len(value) <= _VALUE_CAP:
        return value
    head, sep, tail = value.partition(" (")
    if sep and cell_len(head) <= _VALUE_CAP and cell_len(tail) + 1 <= _VALUE_CAP:
        return f"{head}\n({tail}"
    return "\n".join(textwrap.wrap(value, _VALUE_CAP)) or value


#: Whether this platform's screen is wide enough for a DESCRIPTION lane at all. The
#: PicoCalc's 53 columns are spent by the setting and its value.
_describe: bool = True


@on_platform
def _bind_describe(platform: Platform) -> None:
    """Bind the DESCRIPTION lane to the platform (runs now and on every switch)."""
    global _describe
    _describe = platform.readable_cols >= 72


def _setting_value(
    spec: SettingSpec, snapshot: dict, pending: dict, reveal_pin: bool = False
) -> Text:
    """One setting's VALUE lane: ``current [→ staged]``.

    The staged arrow is drawn in the warn style so a dirty row stands out at a glance;
    the span survives the select highlight (which only tints the row's base style).

    The pairing PIN is concealed here on the same terms as in :func:`config_table` — a
    staged one too: a PIN you typed a moment ago is still a PIN, and a row that uncovered
    itself the instant it was edited would leave the secret on screen for the rest of the
    session's staging.
    """
    hide = spec.key == PIN_KEY and not reveal_pin
    current = spec.getter(snapshot)
    # The repeater page's words for the two non-values: ``?`` never reported, ``empty`` a
    # string the device holds blank — muted, and never parenthesized.
    if current is None:
        value = Text("?", style="muted")
    elif spec.value_type == "str" and current == "":
        value = Text("empty", style="muted")
    else:
        shown = format_value(spec, current)
        value = Text(conceal(shown) if hide else shown)
    if spec.key in pending:
        new = pending[spec.key]
        if spec.value_type == "str" and new == "":
            staged = "empty"
        else:
            staged = format_value(spec, new)
            staged = conceal(staged) if hide else staged
        value.append(f" → {staged}", style="warn")
    return value


class _ConfigMenu(SelectScreen):
    """The editor's list, with the pairing PIN's row concealed until ``^S`` uncovers it.

    Everything a grouped select list does, plus the same toggle the Device info page
    carries — the same chord, the same chip, the same words — because the two pages show
    the same value and a secret that comes out from under a different key on each is a
    second thing to learn for nothing. Here the key *has* to be a chord: this list filters
    as you type, so every bare letter is spoken for.

    The rows are data (each carries its staged ``current → new``), so a toggle refreshes
    them in place through :meth:`~meshterm.ui.tui.select.SelectScreen.replace_items` —
    which follows the highlighted row by value and keeps the typed filter — rather than
    rebuilding the screen under a reader who was part-way down it.

    It is a full-screen page, not a floating popup — the same as the repeater-admin page it
    mirrors: a device's whole configuration, with its actions, is a place the reader works
    in for a while, and the value prompts, confirms and results float over it.
    """

    floating = False

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        build: Callable[[bool], tuple[str, list]],
        *,
        conceals: bool,
        **kwargs: Any,
    ) -> None:
        """Build the list over its row builder.

        Args:
            session: The running TUI session (repainted when the PIN is toggled).
            build: Renders ``(title, items)`` for a given ``reveal_pin``.
            conceals: Whether this device reports a PIN at all. ``False`` leaves the page
                with nothing to reveal, so neither the footer nor the lane offers a key
                for it.
            **kwargs: Passed to :class:`~meshterm.ui.tui.select.SelectScreen`.
        """
        title, items = build(False)
        # The lanes end at the edge rather than slide. The Actions rows pin a head block
        # (menus.menu_rows), which on its own turns ←→ scrolling on for the whole list — and
        # a setting row, pinning nothing, would then slide its label out with its description.
        kwargs.setdefault("hscroll", False)
        super().__init__(title, items, **kwargs)
        self._session = session
        self._build = build
        self._conceals = conceals
        self._revealed = False

    def conceal_pin(self, conceals: bool) -> None:
        """Say whether the device reports a PIN now — a reset or a restore can change it."""
        self._conceals = conceals
        if not conceals:
            self._revealed = False

    def refresh(self) -> None:
        """Re-read the rows for what is staged now, keeping the reveal, cursor and filter."""
        title, items = self._build(self._revealed)
        self.replace_items(items, title=title)

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The list's own hint, plus the reveal atom before the trailing Esc clause."""
        base = super().footer_hint
        if not self._conceals:
            return base
        verb = "hide" if self._revealed else "show"
        return splice_hint(base, f"{REVEAL_KEY} {verb} PIN")

    @property
    def fkey_lane(self):
        """The list's lane with the reveal on F3 — the slot a delete-less list leaves free.

        F1/F2 are this grouped list's section jumps, F4/F5 the pager; F3 is what a select
        list spends on its own verb (``Delete``, where it has one), and this one's verb is
        the reveal. A device with no PIN leaves the slot empty rather than dim: the action
        isn't a thing on this page at all.
        """
        from .tui.fkeys import FPair

        lane = list(super().fkey_lane)
        if self._conceals:
            lane[2] = FPair("Hide" if self._revealed else "Reveal", "reveal")
        return lane

    def handle(self, action: str, data: str = "") -> None:
        """Toggle the PIN, or behave as any select list does."""
        if action == "reveal":
            if not self._conceals:
                return
            self._revealed = not self._revealed
            self.refresh()
            self._session.invalidate()
            return
        super().handle(action, data)


def _menu_items(
    snapshot: dict,
    pending: dict,
    staged: int,
    reveal_pin: bool = False,
    *,
    custom: dict[str, str] | None = None,
    custom_pending: dict[str, str] | None = None,
) -> tuple[str, list]:
    """Build the page's title and rows for the current snapshot + staged state.

    The settings sit in three aligned lanes — setting, current value (and any staged new
    value), description — under one header line, grouped by category; the device's custom
    variables follow in the same lanes; and the **Actions** close the list. The same shape
    as the repeater-admin page (see :func:`meshterm.ui.repeater_admin._menu_items`), so
    the two read as one editor.

    ``reveal_pin`` is :class:`_ConfigMenu`'s toggle, passed straight through to the PIN's
    value lane. ``custom`` holds the variables the device reports and ``custom_pending``
    the values staged for them.
    """
    custom = custom or {}
    custom_pending = custom_pending or {}
    # First pass: collect every row's lanes per category, so the columns can be sized to
    # their content (including any staged ``→ new`` arrows) before a single row is built.
    sections: list[tuple[str, list[tuple[str, Text, str, Any]]]] = []
    for category, specs in settings_by_category():
        rows: list[tuple[str, Text, str, Any]] = []
        for spec in specs:
            if spec.key == "adv_lat":
                # The map pick sets both coordinates at once, so it heads the pair it fills —
                # the same row, in the same place, as on the repeater admin page.
                rows.append((PICK_LOCATION_LABEL, Text(), PICK_LOCATION_HELP, _LOCATION))
            rows.append(
                (
                    spec.label,
                    _setting_value(spec, snapshot, pending, reveal_pin),
                    spec.help,
                    spec.key,
                )
            )
        if category == "Radio":
            rows.append(
                (
                    "Radio presets…",
                    Text(),
                    "Stage MeshCore's settings for a region",
                    _PRESETS,
                )
            )
        sections.append((category, rows))

    # Custom variables: each one the firmware or this board's sensors report, staged like a
    # setting (the firmware's own, GPS, carry a name and a type — see _KNOWN_VARS), plus
    # any new name staged by hand and the row to stage one.
    var_rows: list[tuple[str, Text, str, Any]] = []
    for name in sorted(set(custom) | set(custom_pending)):
        label, help_text, _kind = _KNOWN_VARS.get(name, (name, "A firmware variable", "str"))
        value = _custom_value(name, custom, custom_pending)
        var_rows.append((label, value, help_text, _CUSTOM_VAR + name))
    var_rows.append(("Set by name…", Text(), "Set a raw firmware variable by name", _CUSTOM))
    sections.append(("Custom variables", var_rows))

    label_w = max(cell_len(label) for _, rows in sections for label, _, _, _ in rows)
    value_w = max(cell_len(value.plain) for _, rows in sections for _, value, _, _ in rows)

    # The shared editor header (menus.lane_header): each label over its own lane, clear of
    # the pointer column, abbreviating rather than wrapping where the terminal is too narrow
    # for the full words. It is pinned for the whole list — the lanes mean the same in every
    # category — so a scrolled row keeps both its column header and its section heading
    # overhead (see Screen.sticky_rows).
    items: list = [Separator(lambda w: lane_header(label_w, value_w, w), pinned=True)]
    for category, rows in sections:
        items.append(section_heading(category))
        for label, value, help_text, key in rows:
            items.append(
                Choice(title=lane_row(label, value, help_text, label_w, value_w), value=key)
            )

    # The operations on the box itself, run the moment they are confirmed — the local
    # counterpart of the repeater page's own Actions, built the same way. ↻ and ⚠ are one
    # cell where 🕒 💾 📂 🔐 🔄 are two, so the column is measured once and every mark padded
    # out to it. The one irreversible row is tinted, on its mark (or its words, iconless).
    actions = [
        ("↻", "Read settings", "Re-read every value from the device", _READ),
        ("🕒", "Sync clock…", "Set the device clock from this computer", _SYNC_CLOCK),
        ("💾", "Back up config…", "Write every setting to a TOML file", _BACKUP),
        ("📂", "Restore config…", "Preview or apply a saved TOML backup", _RESTORE),
        ("🔐", "Identity key…", "Export or import the node's private key", _IDENTITY_KEY),
        ("🔄", "Reboot device…", "Restart the companion and reconnect", _REBOOT),
        ("⚠", "Factory reset…", "Erase everything (typed confirmation)", _RESET),
    ]
    lane = icon_lane(icon for icon, _, _, _ in actions)
    items.append(section_heading("Actions"))
    items.extend(
        menu_rows(
            (
                marked_label(icon, label, "err" if value == _RESET else "", lane=lane),
                help_text,
                value,
            )
            for icon, label, help_text, value in actions
        )
    )

    # Nothing at all while the page is clean — Esc leaves, and a row saying so was
    # retired app-wide. With changes staged the pair appears below one blank line: Apply
    # has no key of its own, and Back spells out what leaving costs (see menus.exit_rows).
    items.extend(exit_rows(staged, apply_value=_APPLY, back_value=_CANCEL))

    name = str(snapshot.get("name") or "")
    title = "Device config" + (f" — {name}" if name else "")
    if staged:
        title += f" · {staged} staged"
    return title, items


# --- staging individual changes ----------------------------------------------


async def _stage_setting(
    ctx: AppContext, key: str, snapshot: dict, pending: dict[str, Any]
) -> None:
    """Prompt for one setting's new value and stage it."""
    spec = get_spec(key)
    current = pending.get(key, spec.getter(snapshot))
    value = await _prompt_value(ctx, spec, current, snapshot)
    if value is None:
        return
    # A typed value was checked as it was typed; a picked one (relaying on or off) is
    # checked here — against the radio as staged, not only as the device still holds it.
    complaint = spec.validate(value, {**snapshot, **pending}) if spec.validate else None
    if complaint:
        ctx.ui.note(f"[err]✗[/err] {escape(complaint)}")
        await ctx.ui.present(title=spec.label)
        return
    if value == spec.getter(snapshot):
        pending.pop(key, None)  # set back to the device's value — nothing to change
    else:
        pending[key] = value


def _range_hint(spec: SettingSpec, snapshot: dict) -> str:
    """A muted "allowed values" hint for a numeric prompt.

    Uses the *effective* maximum — the device-reported bound (e.g. this board's max TX
    power) when the spec names one, else the static bound — so the hint promises exactly
    what validation will accept.
    """
    maximum = effective_maximum(spec, snapshot)
    if spec.minimum is not None and maximum is not None:
        return f"Allowed: {spec.minimum:g} – {maximum:g}"
    if spec.minimum is not None:
        return f"Allowed: ≥ {spec.minimum:g}"
    if maximum is not None:
        return f"Allowed: ≤ {maximum:g}"
    return ""


async def _prompt_value(ctx: AppContext, spec: SettingSpec, current: Any, snapshot: dict) -> Any:
    """Prompt for a typed value for ``spec`` (in the fitting dialog), ``None`` on cancel."""
    if spec.value_type == "bool":
        # A straight two-state choice reads best as a button pair; the current state is
        # the highlighted default so Enter changes nothing by accident.
        return await ctx.ui.dialog(
            spec.help,
            [("Off", False), ("On", True)],
            title=spec.label,
            default=1 if current else 0,
            keys={"0": False, "1": True, "n": False, "y": True},
        )

    if spec.value_type == "enum" and spec.choices is not None:
        items: list = [
            Choice(
                title=f"{k} — {label}" + ("  (current)" if k == current else ""),
                value=k,
            )
            for k, label in spec.choices.items()
        ]
        # Non-strict enums list the common values for convenience but still accept any
        # in-range integer, so offer an escape hatch to type one in.
        if not spec.strict_choices:
            items.append(Choice(title="Other (enter a value)…", value=_OTHER))
        selected = await ctx.ui.select(
            spec.label,
            items,
            prompt=spec.help,
            default=current if current in spec.choices else None,
        )
        if selected is None:
            return None
        if selected != _OTHER:
            return selected
        # else: fall through to the free-text prompt below.

    def validate(text: str) -> bool | str:
        try:
            parse_value(spec, text, snapshot)
            return True
        except DeviceConfigError as exc:
            return str(exc)

    raw = await ctx.ui.text(
        spec.label,
        prompt=spec.help,
        default="" if current is None else str(current),
        validate=validate,
        help_text=_range_hint(spec, snapshot),
    )
    return None if raw is None else parse_value(spec, raw, snapshot)


async def _stage_location(ctx: AppContext, snapshot: dict, pending: dict[str, Any]) -> None:
    """Pick the advertised location on the map and stage both coordinates it sets.

    The map opens on the position as staged (or as the device holds it), and on the mesh
    where there is none. Each coordinate keeps its own row for a typed value — ``0`` in both
    is MeshCore's "no fix", and the node stops advertising a position. Staged like any other
    setting: the coordinates only reach the device on Apply.
    """
    from .map_screen import coords_or_none, pick_location

    lat = pending.get("adv_lat", snapshot.get("adv_lat"))
    lon = pending.get("adv_lon", snapshot.get("adv_lon"))
    picked = await pick_location(ctx, initial=coords_or_none(lat, lon))
    if picked is None:
        return
    # Six decimals ≈ 0.1 m — beyond the map's own precision, plenty for an advert.
    for key, value in zip(_COORD_KEYS, picked, strict=True):
        if round(value, 6) == snapshot.get(key):
            pending.pop(key, None)  # the device's own value — nothing to change
        else:
            pending[key] = round(value, 6)


async def _stage_preset(ctx: AppContext, snapshot: dict, pending: dict[str, Any]) -> None:
    """Pick a MeshCore radio preset and stage every field it names for review/apply.

    The rows are MeshCore's own suggested settings (see
    :data:`~meshterm.core.device_config.RADIO_PRESETS`), name in one lane and parameters
    in the other, and the list opens on the preset the radio is already tuned to where
    one matches — the "which of these am I on?" reading the phone app gives, so picking a
    neighbour is a comparison rather than a guess. Staged values count: a preset picked
    after a hand-edited frequency is read against what would be applied, not against what
    the radio still holds.
    """
    from ..core.device_config import RADIO_PRESETS, current_preset

    items = menu_rows([(p.name, p.summary, i) for i, p in enumerate(RADIO_PRESETS)])
    active = current_preset({**snapshot, **pending})
    idx = await ctx.ui.select(
        "Radio presets",
        items,
        prompt="Stage a standard set of radio parameters:",
        default=RADIO_PRESETS.index(active) if active is not None else None,
    )
    if idx is None:
        return
    pending.update(RADIO_PRESETS[idx].as_settings())


async def _stage_custom_var(
    ctx: AppContext, custom: dict[str, str], custom_pending: dict[str, str]
) -> None:
    """Prompt for a custom/experimental variable by name and stage a value for it.

    Known variable names are offered as suggestions so an existing one can be recalled
    without retyping it; any new name is accepted as free text.

    Name then value, as a stack (:func:`~meshterm.ui.menus.run_steps`): Esc on the value
    steps back to the name it is for, rather than dropping both and starting over.
    """
    answers = await run_steps(
        [
            lambda vals: _ask_custom_name(ctx, custom, vals[0]),
            lambda vals: ctx.ui.text(
                "Custom variable",
                prompt=f"Value for {vals[0]}:",
                default=custom.get(vals[0], "") if vals[1] is None else vals[1],
            ),
        ]
    )
    if answers is None:
        return
    key, value = answers
    if value == custom.get(key):
        custom_pending.pop(key, None)  # what the device already holds — nothing to send
    else:
        custom_pending[key] = value


async def _ask_custom_name(
    ctx: AppContext, custom: dict[str, str], previous: str | None
) -> str | None:
    """The variable-name step: suggestions where there are any, free text otherwise.

    Args:
        ctx: Shared application context.
        custom: The variables the device already reports, offered for recall.
        previous: What this step last returned, so coming back to it opens on it.

    Returns:
        The trimmed name, or ``None`` if it was left blank or cancelled.
    """
    prompt = "Name of the firmware variable to set:"
    if custom:
        typed = await ctx.ui.autocomplete(
            "Custom variable", sorted(custom), prompt=prompt, default=previous or ""
        )
    else:
        typed = await ctx.ui.text("Custom variable", prompt=prompt, default=previous or "")
    return typed.strip() if typed and typed.strip() else None


#: Custom variables the companion firmware itself defines (``CMD_SET_CUSTOM_VAR`` in
#: MeshCore's ``examples/companion_radio/MyMesh.cpp``): ``(label, description, kind)``, so
#: their rows read like settings. Anything else a board's sensors report is listed under its
#: own name and edited as text.
_KNOWN_VARS: dict[str, tuple[str, str, str]] = {
    "gps": ("GPS", "Run the GPS receiver (boards with one)", "bool"),
    "gps_interval": ("GPS interval (s)", "Seconds between GPS position reads", "int"),
}

#: The firmware's ceiling on ``gps_interval`` (``constrain(…, 0, 86400)``): one day.
_GPS_INTERVAL_MAX = 86400


def _custom_value(name: str, custom: dict[str, str], custom_pending: dict[str, str]) -> Text:
    """One custom variable's VALUE lane: ``current [→ staged]``, in the settings' words."""

    def shown(raw: str) -> str:
        if _KNOWN_VARS.get(name, ("", "", "str"))[2] == "bool" and raw in ("0", "1"):
            return "on" if raw == "1" else "off"
        return raw if raw != "" else "empty"

    if name not in custom:
        value = Text("?", style="muted")  # staged by name; the device never reported it
    elif custom[name] == "":
        value = Text("empty", style="muted")
    else:
        value = Text(shown(custom[name]))
    if name in custom_pending:
        value.append(f" → {shown(custom_pending[name])}", style="warn")
    return value


async def _stage_var(
    ctx: AppContext, name: str, custom: dict[str, str], custom_pending: dict[str, str]
) -> None:
    """Prompt for one listed custom variable's new value and stage it."""
    label, help_text, kind = _KNOWN_VARS.get(name, (name, "A firmware variable", "str"))
    current = custom_pending.get(name, custom.get(name, ""))
    if kind == "bool":
        picked = await ctx.ui.dialog(
            help_text,
            [("Off", "0"), ("On", "1")],
            title=label,
            default=1 if current == "1" else 0,
            keys={"0": "0", "1": "1", "n": "0", "y": "1"},
        )
        if picked is None:
            return
        value = str(picked)
    else:
        extra: dict[str, Any] = {}
        if kind == "int":
            extra = {
                "validate": _valid_interval,
                "help_text": f"Allowed: 0 – {_GPS_INTERVAL_MAX}",
            }
        raw = await ctx.ui.text(label, prompt=f"Value for {name}:", default=current, **extra)
        if raw is None:
            return
        value = raw.strip()
    if value == custom.get(name):
        custom_pending.pop(name, None)  # back to what the device holds — nothing to send
    else:
        custom_pending[name] = value


def _valid_interval(text: str) -> bool | str:
    """Validate a whole number of seconds within the firmware's GPS-interval bound."""
    try:
        seconds = int(text.strip())
    except ValueError:
        return "Enter a whole number of seconds."
    return True if 0 <= seconds <= _GPS_INTERVAL_MAX else f"Must be 0 – {_GPS_INTERVAL_MAX}."


# --- applying, and re-reading ---------------------------------------------------


def _staged_ops(pending: dict[str, Any], custom_pending: dict[str, str]) -> list[tuple[str, tuple]]:
    """The staged values as ``(stage key, op)`` pairs, in the order they should be sent.

    Settings go in the registry's order rather than the order they were staged in, so a
    retuned frequency lands before relaying is switched on for it; the custom variables
    follow.
    """
    order = {spec.key: i for i, spec in enumerate(DEVICE_SETTINGS)}
    ops = [
        (key, ("set", key, pending[key]))
        for key in sorted((k for k in pending if k in order), key=order.__getitem__)
    ]
    ops += [(name, ("set_custom", name, value)) for name, value in custom_pending.items()]
    return ops


async def _apply(
    ctx: AppContext,
    device: Device,
    snapshot: dict,
    pending: dict[str, Any],
    custom_pending: dict[str, str],
) -> int:
    """Send every staged value, one at a time, and show what the device made of each.

    The local twin of the repeater page's Apply. Each value goes through
    :func:`~meshterm.tools.config.apply_ops` — the executor ``config set`` runs too — in
    :func:`_staged_ops`'s order. One the device takes leaves the stage; one it refuses
    *stays* staged with the reason beside it, to be corrected or unstaged, never silently
    dropped. A lost link is not a refusal: it is re-raised for the app's disconnect
    handling. One ``runs`` row records the batch, naming settings and never their values —
    one of them may be the pairing PIN.

    Returns:
        How many staged values the device accepted.
    """
    from ..core.connection import is_connection_lost
    from ..tools.config import apply_ops

    batch = _staged_ops(pending, custom_pending)
    names = [key for key, _op in batch]
    run_id = ctx.repo.start_run("config", {"mode": "apply", "settings": names}, ctx.profile_name)
    accepted = 0
    for (key, op), name in zip(batch, names, strict=True):
        try:
            await apply_ops(ctx, device, snapshot, [op])
        except Exception as exc:  # noqa: BLE001 - a refusal is shown, and stays staged
            if is_connection_lost(exc):
                ctx.repo.finish_run(run_id, "error", {"applied": accepted, "error": str(exc)})
                raise
            ctx.ui.note(
                f"[err]✗[/err] [brand]{escape(name)}[/brand] — {escape(str(exc))} (still staged)"
            )
            continue
        accepted += 1
        (custom_pending if op[0] == "set_custom" else pending).pop(key, None)
    left = len(batch) - accepted
    ctx.repo.finish_run(
        run_id, "error" if left else "ok", {"applied": accepted, "staged_left": left}
    )
    ctx.ui.note(
        f"[brand]{accepted}[/brand] of {len(batch)} staged changes applied"
        + ("  [warn](the rest stay staged)[/warn]" if left else "")
    )
    await ctx.ui.present(title="Apply")
    return accepted


async def _reread(ctx: AppContext, device: Device) -> tuple[dict, dict[str, str]]:
    """Read the whole device again, after an apply or an action changed it under the page.

    Raw rather than through the session cache, which is exactly what just went stale — and
    the cache is dropped on the way, so the next screen that trusts it reads the truth too.
    """
    from ..tools.config import learn_default_scope

    ctx.devstate.invalidate_config()
    async with ctx.ui.busy_overlay():
        snapshot = await build_snapshot(device)
        custom = await device.get_custom_vars()
    learn_default_scope(ctx, snapshot)
    return snapshot, custom


def _unstage_held(
    snapshot: dict, custom: dict[str, str], pending: dict[str, Any], custom_pending: dict[str, str]
) -> None:
    """Unstage every value the device turns out to hold already — a re-read can settle one."""
    for key in list(pending):
        if get_spec(key).getter(snapshot) == pending[key]:
            del pending[key]
    for name in [n for n, value in custom_pending.items() if custom.get(n) == value]:
        del custom_pending[name]


async def _run_now(
    ctx: AppContext, device: Device, snapshot: dict, ops: list[tuple], title: str
) -> int:
    """Execute ``ops`` on the device right away and show the result window.

    The immediate-action counterpart of the staged Apply path: same executor
    (:func:`~meshterm.tools.config.apply_ops`), so the notes and behavior match, but the
    output is presented at once instead of waiting for the tool to finish.

    Returns:
        The number of changes applied.
    """
    from ..tools.config import apply_ops

    changes, artifacts, _report = await apply_ops(ctx, device, snapshot, ops)
    for artifact in artifacts:
        ctx.ui.note(f"[ok]●[/ok] wrote [accent]{artifact}[/accent]")
    await ctx.ui.present(title=title)
    return changes


async def send_advert(ctx: AppContext) -> None:
    """Run the Send advert flow, behind the main menu's ``advert`` popup tool.

    Send an advert (zero-hop or flood) or show this node's shareable contact card. Either
    advert waits out the transmit cooldown first — a flood advert its own, longer one —
    which is a countdown the reader can cancel where the wait is long enough to notice.
    Only
    ``SELF_INFO`` is read here: the advert command itself never consults the snapshot,
    and the contact card needs just the name, public key, and advert type — so opening
    this skips the tuning/path-hash reads of a full
    :func:`~meshterm.core.device_config.build_snapshot` and stays snappy over Bluetooth.

    Args:
        ctx: Shared application context (provides the connected device and UI surface).
    """
    device = await ctx.device()
    snapshot = dict(await ctx.devstate.self_info())  # the session cache; no re-read
    # No emoji in this floating dialog's title: a terminal that paints an emoji a cell
    # narrower than Rich measures it leaves the content-sized popup's title border short,
    # bleeding the backdrop through the frame. The menu row keeps its 📡 icon (drawn in the
    # full-width base, where the miscount has nowhere to show). filterable=False too: a
    # stray key must not narrow (and so resize) this fixed four-item list.
    choice = await ctx.ui.select(
        "Send advert",
        [
            *menu_rows(
                [
                    ("Zero-hop", "Announce to direct neighbours", "zero"),
                    ("Flood", "Repeaters rebroadcast it across the mesh", "flood"),
                    ("Share QR / URI", "Show this node's contact card", "share"),
                ]
            ),
        ],
        prompt="Announce this node to the mesh:",
        filterable=False,
    )
    if choice is None:
        return
    if choice == "share":
        await _show_contact_card(ctx, snapshot)
        return

    # An advert is the one transmission a reader fires by hand, over and over, so it is
    # where the shared transmit cooldown is felt (see meshterm.ui.cooldown). A wait worth
    # noticing becomes a countdown they can back out of; a short one just happens.
    from .cooldown import wait_for_cooldown

    flood = choice == "flood"
    if not await wait_for_cooldown(
        ctx, action="Flood advert" if flood else "Zero-hop advert", flood_advert=flood
    ):
        return
    await _run_now(ctx, device, snapshot, [("advert", flood)], "Advert")


def contact_share_url(name: str, public_key: str, node_type: int = 1) -> str:
    """Build the MeshCore ``meshcore://contact/add`` share URL for this node.

    The companion-app format (see the MeshCore ``qr_codes`` doc): the advertised name,
    the full 32-byte public key as hex, and the node type (1 = companion, 2 = repeater,
    3 = room server, 4 = sensor).

    Args:
        name: The node's advertised name.
        public_key: The node's public key as a hex string.
        node_type: The MeshCore advert type byte.

    Returns:
        A ``meshcore://contact/add?name=…&public_key=…&type=…`` URL.
    """
    return (
        f"meshcore://contact/add?name={quote(name, safe='')}"
        f"&public_key={public_key.lower()}&type={int(node_type)}"
    )


async def show_contact_card(
    ctx: AppContext, name: str, public_key: str, node_type: int = 1
) -> None:
    """Show a node's contact card: a scannable QR code over the raw share link, full-frame.

    THE share-a-contact screen, used both for our own node (the advert menu's
    ``Share QR / URI``) and for any full-keyed contact (the node detail page's
    ``Share contact``): a phone scans the code — or the link is passed along as text —
    and the companion app adds the node as a contact.

    Args:
        ctx: Shared application context (provides the UI surface).
        name: The node's advertised name (the card's title and the link's name field).
        public_key: The node's full public key as hex (the link is useless without it).
        node_type: The MeshCore advert type byte (1 companion, 2 repeater, 3 room,
            4 sensor).
    """
    from .qr import share_screen

    await share_screen(ctx, name=name, url=contact_share_url(name, public_key, node_type))


async def _show_contact_card(ctx: AppContext, snapshot: dict) -> None:
    """Show our own node's contact card from its ``SELF_INFO`` snapshot."""
    public_key = str(snapshot.get("public_key") or "")
    if not public_key:
        ctx.ui.note("[err]the device did not report a public key — nothing to share[/err]")
        await ctx.ui.present(title="Share contact")
        return
    name = str(snapshot.get("name") or "this node")
    await show_contact_card(ctx, name, public_key, int(snapshot.get("adv_type") or 1))


async def _reboot(ctx: AppContext, device: Device, snapshot: dict) -> bool:
    """Confirm and reboot the device, handing off to the session's reconnect dialog.

    After the command is sent, we wait to actually observe the link dropping — flagging
    :attr:`~meshterm.context.AppContext.reboot_in_progress` so the session-wide
    disconnect watcher labels the ensuing dialog as a reboot, waits for the companion to
    come back, and reconnects — exactly the unplugged-device flow.

    Returns:
        ``True`` if the reboot was sent and the actions screen should close; ``False``
        if the user backed out (or the simulator, which has no link to drop, absorbed it).
    """
    choice = await ctx.ui.dialog(
        "Reboot the device now?",
        [("Cancel", None), ("Reboot", "reboot")],
        title="Reboot device",
        default=1,
        danger=True,
    )
    if choice != "reboot":
        return False

    if ctx.active_transport is None:
        # The simulator has no link to drop and comes back instantly; just send it.
        await device.reboot()
        ctx.ui.note("[warn]device rebooting[/warn]")
        await ctx.ui.present(title="Reboot")
        return False

    # Flag the drop as expected *before* sending, so however quickly the watcher fires,
    # the reconnect dialog already knows to present it as a reboot.
    ctx.reboot_in_progress = True
    try:
        await device.reboot()
    except Exception:
        ctx.reboot_in_progress = False
        raise
    # Hold here until the link is actually observed down (or a generous timeout), so the
    # actions screen doesn't flash back to the menu for the second or two before the
    # watcher notices. The watcher may cancel us mid-wait when it fires — that's the handoff.
    deadline = asyncio.get_running_loop().time() + _REBOOT_DROP_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        if not await ctx.link_alive():
            break
        await asyncio.sleep(_REBOOT_DROP_POLL_S)
    return True


async def _sync_clock(ctx: AppContext, device: Device, snapshot: dict) -> None:
    """Show the device clock's drift against this computer and offer to correct it.

    A companion that boots with a bad RTC stamps every message wrongly, so the dialog
    leads with the measured drift (or admits the clock is unreadable) before the Sync
    button writes the host's time.
    """
    import time

    device_time: int | None = None
    try:
        device_time = await device.get_time()
    except Exception:  # noqa: BLE001 - old firmware; offer the blind sync instead
        pass
    if device_time:
        drift = device_time - int(time.time())
        stamp = _clock_text(device_time)
        prompt = (
            f"The device clock reads {stamp} — {_drift_text(drift)}. Set it from this computer?"
        )
    else:
        prompt = "The device did not report its clock. Set it from this computer anyway?"
    choice = await ctx.ui.dialog(
        prompt,
        [("Cancel", None), ("Sync", "sync")],
        title="Sync clock",
        default=1,
    )
    if choice == "sync":
        await _run_now(ctx, device, snapshot, [("sync_clock",)], "Sync clock")


def _clock_text(epoch: int) -> str:
    """A device timestamp rendered in this computer's local time."""
    from datetime import datetime

    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _drift_text(drift: int) -> str:
    """Describe a clock drift in seconds: ``"12 s behind"``, ``"3 s ahead"``, ``"in sync"``."""
    if abs(drift) < 2:
        return "in sync with this computer"
    direction = "ahead of" if drift > 0 else "behind"
    return f"{abs(drift)} s {direction} this computer"


async def _backup_now(ctx: AppContext, device: Device, snapshot: dict) -> None:
    """Prompt for a destination and write the TOML backup immediately."""
    path = await ctx.ui.path(
        "Back up config",
        prompt="Write every setting to this TOML file:",
        default="meshterm-config.toml",
    )
    if path:
        await _run_now(ctx, device, snapshot, [("backup", Path(path))], "Backup")


async def _restore_now(ctx: AppContext, device: Device, snapshot: dict) -> bool:
    """Restore from a TOML backup: pick the file, preview if wanted, then apply.

    Returns:
        ``True`` if the device was changed (so the caller refreshes its snapshot).
    """
    raw = await ctx.ui.path("Restore config", prompt="Read settings from this TOML backup file:")
    if not raw:
        return False
    path = Path(raw)
    if not path.exists():
        ctx.ui.note(f"[err]no such file:[/err] {path}")
        await ctx.ui.present(title="Restore")
        return False

    choice = await ctx.ui.dialog(
        "Apply the backup now, or preview the changes first?",
        [("Cancel", None), ("Preview", "preview"), ("Apply", "apply")],
        title="Restore from backup",
        default=1,
    )
    if choice == "preview":
        await _run_now(ctx, device, snapshot, [("restore", path, True)], "Restore preview")
        choice = await ctx.ui.dialog(
            "Apply these changes to the device?",
            [("Cancel", None), ("Apply", "apply")],
            title="Restore from backup",
            default=1,
        )
    if choice != "apply":
        return False
    changed = await _run_now(ctx, device, snapshot, [("restore", path, False)], "Restore")
    return changed > 0


async def _identity_key_menu(ctx: AppContext, device: Device, snapshot: dict) -> bool:
    """Export or import the device's private identity key.

    Returns:
        ``True`` if the identity changed (a key was imported), so the caller re-reads
        its snapshot.
    """
    choice = await ctx.ui.select(
        "Identity key",
        [
            *menu_rows(
                [
                    ("Show private key", "Display it on screen (sensitive)", "show"),
                    ("Export to a file…", "Write it to disk (keep it secret)", "file"),
                    ("Import a key…", "Replace this device's identity", "import"),
                ]
            ),
        ],
        prompt="Manage this node's private identity key:",
    )
    if choice is None:
        return False

    if choice == "show":
        ok = await ctx.ui.dialog(
            "The private key IS the node's identity — anyone who sees it can impersonate "
            "this node. Show it on screen?",
            [("Cancel", None), ("Show key", "show")],
            title="Show private key",
            default=1,
            danger=True,
        )
        if ok == "show":
            await _run_now(ctx, device, snapshot, [("export_key",)], "Private key")
        return False

    if choice == "file":
        path = await ctx.ui.path(
            "Export identity key",
            prompt="Write the private key to this file (keep it secret):",
            default="meshterm-identity.key",
        )
        if path:
            await _run_now(ctx, device, snapshot, [("export_key", Path(path))], "Private key")
        return False

    # Import: collect the key, then gate behind the typed confirmation.
    key_hex = await ctx.ui.text(
        "Import identity key",
        prompt="Paste the private key as hex:",
        validate=_is_hex,
    )
    if not key_hex:
        return False
    confirmed = await ctx.ui.typed_confirm(
        "Importing a key permanently overwrites this device's identity. Contacts and "
        "messages keyed to the old identity will no longer match it.",
        "IMPORT",
        title="Import private key",
    )
    if not confirmed:
        return False
    await _run_now(ctx, device, snapshot, [("import_key", key_hex.strip())], "Import key")
    return True


async def _factory_reset(ctx: AppContext, device: Device, snapshot: dict) -> bool:
    """Factory-reset the device behind a typed confirmation.

    Returns:
        ``True`` if the reset ran (so the caller drops everything it staged and re-reads
        the device).
    """
    confirmed = await ctx.ui.typed_confirm(
        "This erases EVERYTHING on the device — identity, contacts, channels, and every "
        "setting — and cannot be undone.",
        "RESET",
        title="Factory reset",
    )
    if not confirmed:
        return False
    await _run_now(ctx, device, snapshot, [("factory_reset",)], "Factory reset")
    return True


# --- validators --------------------------------------------------------------


def _is_hex(text: str) -> bool | str:
    """Validate that ``text`` is a hex string."""
    try:
        bytes.fromhex(text)
        return True
    except ValueError:
        return "Enter hex characters only."
