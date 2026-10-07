# SPDX-License-Identifier: Apache-2.0
"""The Device config page: all the settings of the companion, and the operations on the device.

This page is behind the ``config`` tool (:func:`edit_config`). On purpose, its shape is the
same as the repeater-admin page (:mod:`meshterm.ui.repeater_admin`): the same full-screen
list, the same setting, value, and description lanes under category headings, staged
``current → new`` values, Apply on the page, and an **Actions** section at the end of the
list. Thus the configuration of the device in your hand looks the same as the
configuration of a repeater over the mesh.

* **Settings**: each value that the companion firmware lets an app read and write (refer
  to :data:`~meshterm.core.device_config.DEVICE_SETTINGS`), and each custom variable that
  the device reports. When the user edits a row, the new value is *staged*. Nothing goes
  to the device until *Apply*. Apply sends the staged values one at a time, and the page
  stays open. If the device refuses a value, that value stays staged with its reason, the
  same as on the remote page. If the user leaves with staged changes, the page asks
  before it discards them.
* **Actions**: the operations on the device itself, not on a value: re-read, clock sync,
  backup and restore, the identity key, reboot, and factory reset. These *run
  immediately*. Each action except the re-read is behind its own prompt or confirmation
  (the destructive actions are behind a typed word). The actions use the same snapshot
  and the same :func:`~meshterm.tools.config.apply_ops` executor as Apply. The everyday
  advert has its own entry in the main menu instead (the ``advert`` tool, through
  :func:`send_advert`). It is one key press away, as a dialog over the menu.

The user selects multiple-choice values in dialogs (booleans as a pair of On and Off
buttons, enums as a floating select list). The user can set the location of the node with
a point on the full-screen map (refer to :class:`~meshterm.ui.map_screen.LocationPickScreen`).
A reboot goes to the same reconnect dialog that the app shows when a device is unplugged.
(Channels have their own full manager: refer to the ``channels`` tool.)
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

# Sentinels for menu actions (different from setting keys, which are plain strings).
_LOCATION = "__location__"
_PRESETS = "__presets__"
_CUSTOM = "__custom__"
#: The prefix of the row value of a custom variable. The name of the variable follows it.
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
# The sentinel for the "enter a value myself" choice, on prompts for enums that are not
# strict.
_OTHER = "__other__"

#: The setting keys that the map selection sets together (each one also has its own row,
#: for a typed value).
_COORD_KEYS = ("adv_lat", "adv_lon")

#: How long the reboot flow holds the page after it announces the reboot (seconds). It
#: waits for the disconnect watcher of the session to cancel it and to open the reconnect
#: dialog. The watcher wakes on the announcement itself. Thus this time runs out only
#: where no watcher runs.
_REBOOT_HANDOFF_TIMEOUT_S = 5.0


async def cached_snapshot(ctx: AppContext, device: Device) -> dict:
    """A snapshot of the device settings for a screen that opens, from the devstate cache.

    Devstate already holds two facts: ``SELF_INFO`` and the path-hash mode. To ask the
    device for them again is the slowest part of
    :func:`~meshterm.core.device_config.build_snapshot`. Each apply of the settings
    invalidates them (refer to ``tools/config.py``), so a screen that opens can trust the
    cache. The refresh after a restore, a key change, or a factory reset still calls
    ``build_snapshot(device)`` directly, because there the state of the device really
    changed.
    """
    self_info: dict | None = None
    path_hash_mode: int | None = None
    try:
        self_info = await ctx.devstate.self_info()
        path_hash_mode = await ctx.devstate.path_hash_mode()
    except Exception:  # noqa: BLE001 - use the raw reads below instead
        pass
    from ..tools.config import learn_default_scope

    snapshot = await build_snapshot(device, self_info=self_info, path_hash_mode=path_hash_mode)
    learn_default_scope(ctx, snapshot)
    return snapshot


async def edit_config(ctx: AppContext) -> dict[str, Any] | None:
    """Run the Device config page until the user leaves it.

    There is one screen for the full visit. Its rows change in place after each round:
    they have the staged ``current → new`` values, and the title counts them. Thus the
    content changes under a highlight and a typed filter that stay where the user put
    them. Each sub-prompt floats over the page. Apply sends the staged values, and the
    page stays (refer to :func:`_apply`). Each action runs where the user selects it. Esc
    leaves, and asks first while some values are still staged. A reboot closes the page
    and gives control to the reconnect dialog of the session.

    Args:
        ctx: The shared application context (gives the connected device and the UI
            surface).

    Returns:
        ``{"applied": n}`` when staged values got to the device during the visit, or
        ``None`` when none did.
    """
    from .tui import CANCEL

    device = await ctx.device()
    snapshot = await cached_snapshot(ctx, device)
    custom = await device.get_custom_vars()

    session = getattr(ctx.ui, "session", None)
    if session is None:  # pragma: no cover - the only caller is in the menu
        raise RuntimeError("the config editor is only available in the menu")
    pending: dict[str, Any] = {}  # setting key -> staged new value
    custom_pending: dict[str, str] = {}  # custom variable name -> staged new value
    applied = 0

    def staged() -> int:
        return len(pending) + len(custom_pending)

    def summary() -> dict[str, Any] | None:
        return {"applied": applied} if applied else None

    # The rows read ``snapshot`` and ``custom`` through this closure. Thus the next refresh
    # draws exactly what a re-read binds to them.
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
                    continue  # continue to edit: the same page, at the same place in it
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
                # A reboot ends the visit, and the staged values go with it. Thus ask first.
                # If the user agrees to the discard, the values are discarded, whatever the
                # reboot does.
                if staged():
                    if not await confirm_discard(ctx, staged(), verb="applying"):
                        continue
                    pending.clear()
                    custom_pending.clear()
                if await _reboot(ctx, device, snapshot):
                    return summary()  # the link goes down, and the reconnect dialog takes control
            elif choice == _RESET:
                if await _factory_reset(ctx, device, snapshot):
                    # All the staged values were staged against a device that does not exist
                    # now.
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
    """Build the full table of the settings, with the staged changes over it.

    Args:
        snapshot: The device snapshot from ``build_snapshot``.
        custom: The current custom variables.
        pending: Optional staged changes (setting key -> new value).
        reveal_pin: ``True`` to print the BLE pairing PIN. By default, it is concealed.
            This table is the dump of the full device: people read it over a shoulder,
            take screenshots of it, and paste it into a bug report. And a pairing code is
            the one value in it that lets the phone of another person connect to the
            device. The Device info page shows it on a key press (refer to
            :class:`~meshterm.ui.device_info_screen.DeviceInfoScreen`). The CLI shows it
            only as one value, with ``meshterm config get device_pin``.

    Returns:
        A Rich :class:`Table` with the current (and staged) value of each setting, and
        the custom variables, ready for ``ctx.ui.show`` or ``ctx.ui.view``.
    """
    pending = pending or {}
    show_staged = bool(pending)
    # The DESCRIPTION lane must have a screen that is wide enough for text next to the
    # setting and its value or values: 72 cells, and no third narrow lane. Where it does
    # not fit, the table has only the settings (at those widths, Rich already removed the
    # column completely), and the explanations stay in the editor, one row at a time.
    describe = _describe and not show_staged
    # The same as the contacts list: a SIMPLE_HEAD table with no frame, a left-justified
    # accent title, and muted headers. Thus the two screens look like one family (refer to
    # widgets.contacts_table).
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
    # The two narrow lanes are ``no_wrap``, and they are folded first to a cap (refer to
    # _fold_label and _fold_value). Thus each lane takes exactly the width of its cap, and
    # DESCRIPTION (the lane whose content is always the widest) keeps all the remaining
    # space. When the labels and the values had their natural size, the descriptions got
    # 19 cells of a 72-cell screen, and almost each description wrapped to three lines.
    table.add_column("SETTING", style="muted", no_wrap=True)
    table.add_column("CURRENT", no_wrap=True)
    if show_staged:
        table.add_column("STAGED", style="warn", no_wrap=True)
    if describe:
        table.add_column("DESCRIPTION", style="muted")

    def row_of(cells: list[str], description: str) -> list[str]:
        """One table row: the narrow lanes, and the description where it is drawn."""
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


#: The one setting that this table conceals: the BLE pairing PIN. The firmware reports it
#: as ``ble_pin``, and :data:`~meshterm.core.device_config.DEVICE_SETTINGS` gives it the
#: key ``device_pin``.
PIN_KEY = "device_pin"


#: The width of a concealed PIN: the width of the field, from the upper bound of the
#: setting itself (999999), not the length of the value in it. A typed password has one
#: mask for each character, because the person who types must count the characters. A
#: stored PIN shows its field. Thus nobody can read from the row how many digits to guess.
#: Also, a PIN of ``0`` behind one bullet looks like a stray dot, not like a concealed
#: value.
_PIN_WIDTH = len(str(get_spec(PIN_KEY).maximum))


def conceal(value: str, *, absent: str = "?") -> str:
    """``value`` as a row of :data:`_PIN_WIDTH` mask bullets.

    A value that the device could not report is not a secret. It is an absence:
    ``format_value`` already renders it as ``?``. A ``?`` behind bullets says that there
    is something to show when there is nothing. Thus only a real value is concealed.
    :func:`has_pin` asks the same question in the opposite direction: a page with nothing
    to conceal offers no keyboard key for it.

    Args:
        value: The value to mask, already rendered.
        absent: How this surface writes "the device never reported one". The token of the
            menu is ``?``. The token of the scripted CLI is
            :data:`~meshterm.ui.script.NONE`. The rule is the same on the two surfaces,
            but the glyph is not, so the caller names it. A ``conceal`` that knew only the
            token of the menu also masked the absence in the CLI, and six bullets told the
            user that the device had a PIN lock when it did not.
    """
    return value if value == absent else MASK_MARK * _PIN_WIDTH


def has_pin(snapshot: dict) -> bool:
    """``True`` if ``snapshot`` has a PIN, that is, if a concealed PIN has a meaning."""
    return get_spec(PIN_KEY).getter(snapshot) is not None


#: The cell caps for the two narrow lanes of the table, with the indent. They have the
#: size of the widest label and value of the settings, without the few longest. The four
#: that overflow fold onto a second line. This costs nothing on rows whose description
#: wraps in all cases, and it gives each other row ten more cells of text.
_SETTING_CAP = 24
_VALUE_CAP = 15

#: The name of each setting is indented two spaces, so that the rows look like they are
#: under their accent section heading, which stays flush left. A folded continuation line
#: is indented two more spaces. Thus a wrapped label never looks like a heading at
#: column 0.
_INDENT = "  "
_HANG = "    "


def _fold_label(label: str, describe: bool) -> str:
    """One SETTING cell: indented, and hard-wrapped at :data:`_SETTING_CAP` if necessary.

    The cap only gives space to the DESCRIPTION lane. Thus where that lane is not drawn,
    the label keeps its natural width: nothing else uses the cells.
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
    """One CURRENT or STAGED cell, hard-wrapped at :data:`_VALUE_CAP`.

    An enum value shows as ``N (label)``. Thus the fold prefers the break before the label
    in parentheses: ``1`` above ``(2-byte hashes)``, not a torn ``1 (2-byte``. And a long
    node name (the field is 31 bytes wide) folds, and does not make the lane as wide as
    the screen.
    """
    if not describe or cell_len(value) <= _VALUE_CAP:
        return value
    head, sep, tail = value.partition(" (")
    if sep and cell_len(head) <= _VALUE_CAP and cell_len(tail) + 1 <= _VALUE_CAP:
        return f"{head}\n({tail}"
    return "\n".join(textwrap.wrap(value, _VALUE_CAP)) or value


#: ``True`` if the screen of this platform is wide enough for a DESCRIPTION lane. On the
#: PicoCalc, the setting and its value use all the 53 cells.
_describe: bool = True


@on_platform
def _bind_describe(platform: Platform) -> None:
    """Bind the DESCRIPTION lane to the platform (now, and at each platform change)."""
    global _describe
    _describe = platform.readable_cols >= 72


def _setting_value(
    spec: SettingSpec, snapshot: dict, pending: dict, reveal_pin: bool = False
) -> Text:
    """The VALUE lane of one setting: ``current [→ staged]``.

    The staged arrow is drawn in the warn style, so that a row with a staged change is
    easy to see. The span stays under the select highlight (which tints only the base
    style of the row).

    The pairing PIN is concealed here on the same terms as in :func:`config_table`, also
    a staged PIN. A PIN that you typed a short time ago is still a PIN. A row that shows
    its secret at the moment when the user edits it leaves the secret on the screen for
    the rest of the staging in the session.
    """
    hide = spec.key == PIN_KEY and not reveal_pin
    current = spec.getter(snapshot)
    # The words of the repeater page for the two non-values: ``?`` for never reported, and
    # ``empty`` for a string that the device holds blank. They are muted, and never in
    # parentheses.
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
    """The list of the editor, with the row of the pairing PIN concealed until ``^S`` shows it.

    It does all that a grouped select list does. It also has the same toggle as the
    Device info page: the same chord, the same chip, and the same words. The two pages
    show the same value. A secret that comes out under a different key on each page is a
    second thing to learn, with no benefit. Here the key must be a chord: this list
    filters as you type, so each bare letter already has a use.

    The rows are data (each row has its staged ``current → new``). Thus a toggle changes
    them in place through :meth:`~meshterm.ui.tui.select.SelectScreen.replace_items`,
    which follows the highlighted row by value and keeps the typed filter. The toggle does
    not build the screen again under a user who was part of the way down it.

    It is a full-screen page, not a floating dialog, the same as the repeater-admin page
    that it copies. All the settings of a device, with its actions, are a place where the
    user works for some time. The value prompts, the confirms, and the results float over
    it.
    """

    floating = False

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to prevent an import cycle
        build: Callable[[bool], tuple[str, list]],
        *,
        conceals: bool,
        **kwargs: Any,
    ) -> None:
        """Build the list over its row builder.

        Args:
            session: The running TUI session (it paints again when the PIN is toggled).
            build: Renders ``(title, items)`` for a given ``reveal_pin``.
            conceals: ``True`` if this device reports a PIN. ``False`` gives the page
                nothing to show, so the footer and the lane offer no keyboard key for it.
            **kwargs: Passed to :class:`~meshterm.ui.tui.select.SelectScreen`.
        """
        title, items = build(False)
        # The lanes end at the edge, and do not slide. The Actions rows pin a head block
        # (menus.menu_rows). That alone turns on ←→ scrolling for the whole list. If ←→
        # scrolling is on, a setting row, which pins nothing, slides its label out with
        # its description.
        kwargs.setdefault("hscroll", False)
        super().__init__(title, items, **kwargs)
        self._session = session
        self._build = build
        self._conceals = conceals
        self._revealed = False

    def conceal_pin(self, conceals: bool) -> None:
        """Set if the device reports a PIN now. A reset or a restore can change it."""
        self._conceals = conceals
        if not conceals:
            self._revealed = False

    def refresh(self) -> None:
        """Build the rows again from the staged values. Keep the reveal, highlight, and filter."""
        title, items = self._build(self._revealed)
        self.replace_items(items, title=title)

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The hint of the list itself, with the reveal atom before the Esc atom at the end."""
        base = super().footer_hint
        if not self._conceals:
            return base
        verb = "hide" if self._revealed else "show"
        return splice_hint(base, f"{REVEAL_KEY} {verb} PIN")

    @property
    def picocalc_lyra_lane(self):
        """The lane of the list, with the reveal on F3, which a list with no Delete leaves free.

        F1 and F2 are the section jumps of this grouped list, and F4 and F5 are the pager.
        A select list uses F3 for its own verb (``Delete``, where it has one), and the verb
        of this list is the reveal. A device with no PIN leaves the slot empty, not dim,
        because the action does not exist on this page.
        """
        from .tui.fkeys import FPair

        lane = list(super().picocalc_lyra_lane)
        if self._conceals:
            lane[2] = FPair("Hide" if self._revealed else "Reveal", "reveal")
        return lane

    def handle(self, action: str, data: str = "") -> None:
        """Toggle the PIN, or act the same as each select list."""
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
    """Build the title and the rows of the page for the current snapshot and staged state.

    The settings are in three aligned lanes (the setting, the current value with any
    staged new value, and the description) under one header line, grouped by category.
    The custom variables of the device follow, in the same lanes. The **Actions** are at
    the end of the list. This is the same shape as the repeater-admin page (refer to
    :func:`meshterm.ui.repeater_admin._menu_items`), so that the two pages look like one
    editor.

    ``reveal_pin`` is the toggle of :class:`_ConfigMenu`, passed directly to the value
    lane of the PIN. ``custom`` holds the variables that the device reports, and
    ``custom_pending`` holds the values staged for them.
    """
    custom = custom or {}
    custom_pending = custom_pending or {}
    # First pass: collect the lanes of each row for each category. Thus the columns get the
    # size of their content (with any staged ``→ new`` arrows) before any row is built.
    sections: list[tuple[str, list[tuple[str, Text, str, Any]]]] = []
    for category, specs in settings_by_category():
        rows: list[tuple[str, Text, str, Any]] = []
        for spec in specs:
            if spec.key == "adv_lat":
                # The map selection sets the two coordinates at the same time, so it comes
                # before the pair that it fills. It is the same row, at the same place, as on
                # the repeater admin page.
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

    # Custom variables: each variable that the firmware or the sensors of this board report,
    # staged the same as a setting. (The variables of the firmware itself, for GPS, have a
    # name and a type: refer to _KNOWN_VARS.) Also each new name staged by hand, and the
    # row to stage one.
    var_rows: list[tuple[str, Text, str, Any]] = []
    for name in sorted(set(custom) | set(custom_pending)):
        label, help_text, _kind = _KNOWN_VARS.get(name, (name, "A firmware variable", "str"))
        value = _custom_value(name, custom, custom_pending)
        var_rows.append((label, value, help_text, _CUSTOM_VAR + name))
    var_rows.append(("Set by name…", Text(), "Set a raw firmware variable by name", _CUSTOM))
    sections.append(("Custom variables", var_rows))

    label_w = max(cell_len(label) for _, rows in sections for label, _, _, _ in rows)
    value_w = max(cell_len(value.plain) for _, rows in sections for _, value, _, _ in rows)

    # The shared editor header (menus.lane_header): each label is above its own lane, clear
    # of the pointer column. Where the terminal is too narrow for the full words, a label is
    # abbreviated, not wrapped. The header is pinned for the full list, because the lanes
    # have the same meaning in each category. Thus a scrolled row keeps its column header
    # and its section heading above it (refer to Screen.sticky_rows).
    items: list = [Separator(lambda w: lane_header(label_w, value_w, w), pinned=True)]
    for category, rows in sections:
        items.append(section_heading(category))
        for label, value, help_text, key in rows:
            items.append(
                Choice(title=lane_row(label, value, help_text, label_w, value_w), value=key)
            )

    # The operations on the device itself. They run immediately when the user confirms
    # them. They are the local counterpart of the Actions of the repeater page, built in the
    # same way. ↻ and ⚠ are one cell wide, and 🕒 💾 📂 🔐 🔄 are two. Thus the column is
    # measured once, and each mark is padded to it. The one irreversible row is tinted, on
    # its mark (or on its words, when there are no icons).
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

    # No rows while nothing is staged: Esc leaves, and we removed the row that said so from
    # the full app. When changes are staged, the pair shows below one blank line. Apply has
    # no keyboard key of its own, and Back says what the user loses if they leave (refer
    # to menus.exit_rows).
    items.extend(exit_rows(staged, apply_value=_APPLY, back_value=_CANCEL))

    name = str(snapshot.get("name") or "")
    title = "Device config" + (f" — {name}" if name else "")
    if staged:
        title += f" · {staged} staged"
    return title, items


# --- stage each change -------------------------------------------------------


async def _stage_setting(
    ctx: AppContext, key: str, snapshot: dict, pending: dict[str, Any]
) -> None:
    """Ask for the new value of one setting, and stage it."""
    spec = get_spec(key)
    current = pending.get(key, spec.getter(snapshot))
    value = await _prompt_value(ctx, spec, current, snapshot)
    if value is None:
        return
    # A typed value was checked while the user typed it. A selected value (relaying on or
    # off) is checked here, against the settings as staged, not only as the device still
    # holds them.
    complaint = spec.validate(value, {**snapshot, **pending}) if spec.validate else None
    if complaint:
        ctx.ui.note(f"[err]✗[/err] {escape(complaint)}")
        await ctx.ui.present(title=spec.label)
        return
    if value == spec.getter(snapshot):
        pending.pop(key, None)  # set back to the value of the device: nothing to change
    else:
        pending[key] = value


def _range_hint(spec: SettingSpec, snapshot: dict) -> str:
    """A muted hint with the range of values (``Allowed: …``) for a numeric prompt.

    It uses the *effective* maximum: the bound that the device reports (for example, the
    maximum TX power of this board) when the spec names one, else the static bound. Thus
    the hint promises exactly what the validation will accept.
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
    """Ask for a typed value for ``spec`` (in the correct dialog). ``None`` on cancel."""
    if spec.value_type == "bool":
        # A simple choice of two states is clearest as a pair of buttons. The current state
        # is the highlighted default, so that Enter does not change a value by accident.
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
        # An enum that is not strict lists the common values to help the user, but it still
        # accepts each integer in the range. Thus offer a way to type one.
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
        # else: continue to the free-text prompt below.

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
    """Select the advertised location on the map, and stage the two coordinates that it sets.

    The map opens on the staged position (or on the position that the device holds). If
    there is no position, the map opens on the mesh. Each coordinate keeps its own row for
    a typed value: ``0`` in the two rows is the "no fix" of MeshCore, and then the node no
    longer advertises a position. The coordinates are staged the same as each other
    setting: they go to the device only on Apply.
    """
    from .map_screen import coords_or_none, pick_location

    lat = pending.get("adv_lat", snapshot.get("adv_lat"))
    lon = pending.get("adv_lon", snapshot.get("adv_lon"))
    picked = await pick_location(ctx, initial=coords_or_none(lat, lon))
    if picked is None:
        return
    # Six decimals ≈ 0.1 m: more than the precision of the map, and sufficient for an advert.
    for key, value in zip(_COORD_KEYS, picked, strict=True):
        if round(value, 6) == snapshot.get(key):
            pending.pop(key, None)  # the value of the device itself: nothing to change
        else:
            pending[key] = round(value, 6)


async def _stage_preset(ctx: AppContext, snapshot: dict, pending: dict[str, Any]) -> None:
    """Select a MeshCore radio preset, and stage each field that it names, to examine and apply.

    The rows are the settings that MeshCore itself suggests (refer to
    :data:`~meshterm.core.device_config.RADIO_PRESETS`), with the name in one lane and the
    parameters in the other. If a preset agrees with the current tuning of the radio, the
    list opens on that preset. This is the "which of these am I on?" information that the
    phone app gives. Thus to select a neighbour is a comparison, not a guess. Staged values
    count: if the user edited the frequency by hand and then selects a preset, the preset
    is compared with the values that Apply will send, not with the values that the radio
    still holds.
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
    """Ask for the name of a custom or experimental variable, and stage a value for it.

    The known variable names are offered as suggestions, so that the user can recall an
    existing name without typing it again. Each new name is accepted as free text.

    The name, then the value, as a stack (:func:`~meshterm.ui.menus.run_steps`). Esc on
    the value goes back to the name that it is for. It does not discard the two answers
    and start again.
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
        custom_pending.pop(key, None)  # the device already holds this value: nothing to send
    else:
        custom_pending[key] = value


async def _ask_custom_name(
    ctx: AppContext, custom: dict[str, str], previous: str | None
) -> str | None:
    """The step for the variable name: suggestions where there are some, else free text.

    Args:
        ctx: The shared application context.
        custom: The variables that the device already reports, offered for recall.
        previous: The value that this step returned the last time. When the user comes
            back to this step, it opens on that value.

    Returns:
        The trimmed name, or ``None`` if it was blank or cancelled.
    """
    prompt = "Name of the firmware variable to set:"
    if custom:
        typed = await ctx.ui.autocomplete(
            "Custom variable", sorted(custom), prompt=prompt, default=previous or ""
        )
    else:
        typed = await ctx.ui.text("Custom variable", prompt=prompt, default=previous or "")
    return typed.strip() if typed and typed.strip() else None


#: The custom variables that the companion firmware itself defines (``CMD_SET_CUSTOM_VAR``
#: in ``examples/companion_radio/MyMesh.cpp`` of MeshCore): ``(label, description, kind)``,
#: so that their rows look like settings. Each other variable that the sensors of a board
#: report is listed under its own name, and edited as text.
_KNOWN_VARS: dict[str, tuple[str, str, str]] = {
    "gps": ("GPS", "Run the GPS receiver (boards with one)", "bool"),
    "gps_interval": ("GPS interval (s)", "Seconds between GPS position reads", "int"),
}

#: The maximum of the firmware for ``gps_interval`` (``constrain(…, 0, 86400)``): one day.
_GPS_INTERVAL_MAX = 86400


def _custom_value(name: str, custom: dict[str, str], custom_pending: dict[str, str]) -> Text:
    """The VALUE lane of a custom variable: ``current [→ staged]``, in the words of a setting."""

    def shown(raw: str) -> str:
        if _KNOWN_VARS.get(name, ("", "", "str"))[2] == "bool" and raw in ("0", "1"):
            return "on" if raw == "1" else "off"
        return raw if raw != "" else "empty"

    if name not in custom:
        value = Text("?", style="muted")  # staged by name, and the device never reported it
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
    """Ask for the new value of one listed custom variable, and stage it."""
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
        custom_pending.pop(name, None)  # back to the value of the device: nothing to send
    else:
        custom_pending[name] = value


def _valid_interval(text: str) -> bool | str:
    """Check for a whole number of seconds inside the GPS-interval bound of the firmware."""
    try:
        seconds = int(text.strip())
    except ValueError:
        return "Enter a whole number of seconds."
    return True if 0 <= seconds <= _GPS_INTERVAL_MAX else f"Must be 0 – {_GPS_INTERVAL_MAX}."


# --- apply, and read again ------------------------------------------------------


def _staged_ops(pending: dict[str, Any], custom_pending: dict[str, str]) -> list[tuple[str, tuple]]:
    """The staged values as ``(stage key, op)`` pairs, in the order in which to send them.

    The settings go in the order of the registry, not in the order in which the user
    staged them. Thus a new frequency gets to the device before relaying is turned on for
    it. The custom variables come after the settings.
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
    """Send each staged value, one at a time, and show the answer of the device to each one.

    This is the local copy of the Apply of the repeater page. Each value goes through
    :func:`~meshterm.tools.config.apply_ops` (the executor that ``config set`` also runs),
    in the order of :func:`_staged_ops`. A value that the device accepts leaves the stage.
    A value that the device refuses *stays* staged, with the reason next to it, so that
    the user can correct it or unstage it. It is never removed silently. A lost link is
    not a refusal: it is raised again for the disconnect handling of the app. One ``runs``
    row stores the batch. It names the settings, but never their values, because one of
    them can be the pairing PIN.

    Returns:
        The number of staged values that the device accepted.
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
    """Read the full device again, after an apply or an action changed it under the page.

    The read goes directly to the device, not through the session cache, because that
    cache is exactly what became stale. Also, this function invalidates the cache, so that
    the next screen that trusts it also reads the correct values.
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
    """Unstage each value that the device already holds. A re-read can find such a value."""
    for key in list(pending):
        if get_spec(key).getter(snapshot) == pending[key]:
            del pending[key]
    for name in [n for n, value in custom_pending.items() if custom.get(n) == value]:
        del custom_pending[name]


async def _run_now(
    ctx: AppContext, device: Device, snapshot: dict, ops: list[tuple], title: str
) -> int:
    """Do ``ops`` on the device immediately, and show the results.

    This is the counterpart of the staged Apply path, for an action that runs immediately.
    It uses the same executor (:func:`~meshterm.tools.config.apply_ops`), so the notes and
    the behaviour are the same. But the output shows immediately, and does not wait until
    the tool finishes.

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
    """Run the Send advert flow, behind the ``advert`` dialog tool of the main menu.

    Send an advert (zero-hop or flood), or show the contact card of this node, which the
    user can share. Each advert first waits for the end of the transmit cooldown (a flood
    advert waits for its own, longer cooldown). When the wait is long enough to notice, it
    is a countdown that the user can cancel. Only ``SELF_INFO`` is read here. The advert
    command itself never uses the snapshot, and the contact card uses only the name, the
    public key, and the advert type. Thus this flow does not do the tuning and path-hash
    reads of a full :func:`~meshterm.core.device_config.build_snapshot`, and it stays
    fast over Bluetooth.

    Args:
        ctx: The shared application context (gives the connected device and the UI
            surface).
    """
    device = await ctx.device()
    snapshot = dict(await ctx.devstate.self_info())  # the session cache, with no new read
    # No emoji in the title of this floating dialog. A terminal can paint an emoji one
    # cell narrower than Rich measures it. Then the title border of the dialog, which has
    # the size of its content, is too short, and the backdrop shows through the border.
    # The menu row keeps its 📡 icon (drawn in the full-width base, where the wrong
    # measurement cannot show). Also filterable=False: a stray key must not make this fixed
    # list of three items narrower (and thus change its size).
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

    # An advert is the one transmission that a user sends by hand, again and again. Thus the
    # user feels the shared transmit cooldown here (refer to meshterm.ui.cooldown). A wait
    # that is long enough to notice becomes a countdown that the user can cancel. A short
    # wait occurs with no message.
    from .cooldown import wait_for_cooldown

    flood = choice == "flood"
    if not await wait_for_cooldown(
        ctx, action="Flood advert" if flood else "Zero-hop advert", flood_advert=flood
    ):
        return
    await _run_now(ctx, device, snapshot, [("advert", flood)], "Advert")


def contact_share_url(name: str, public_key: str, node_type: int = 1) -> str:
    """Build the MeshCore ``meshcore://contact/add`` share URL for this node.

    The format of the companion app (refer to the MeshCore ``qr_codes`` document): the
    advertised name, the full 32-byte public key as hex, and the node type
    (1 = companion, 2 = repeater, 3 = room server, 4 = sensor).

    Args:
        name: The advertised name of the node.
        public_key: The public key of the node, as a hex string.
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
    """Show the contact card of a node, full-frame: a scannable QR code above the raw share link.

    This is the only screen that shares a contact. It is used for our node
    (``Share QR / URI`` in the advert menu), and for each contact with a full key
    (``Share contact`` on the node detail page). A phone scans the code (or the user
    gives the link to someone as text), and the companion app adds the node as a
    contact.

    Args:
        ctx: The shared application context (gives the UI surface).
        name: The advertised name of the node (the title of the card, and the name field
            of the link).
        public_key: The full public key of the node, as hex (the link has no use without
            it).
        node_type: The MeshCore advert type byte (1 companion, 2 repeater, 3 room
            server, 4 sensor).
    """
    from .qr import share_screen

    await share_screen(ctx, name=name, url=contact_share_url(name, public_key, node_type))


async def _show_contact_card(ctx: AppContext, snapshot: dict) -> None:
    """Show the contact card of our node, from its ``SELF_INFO`` snapshot."""
    public_key = str(snapshot.get("public_key") or "")
    if not public_key:
        ctx.ui.note("[err]the device did not report a public key — nothing to share[/err]")
        await ctx.ui.present(title="Share contact")
        return
    name = str(snapshot.get("name") or "this node")
    await show_contact_card(ctx, name, public_key, int(snapshot.get("adv_type") or 1))


async def _reboot(ctx: AppContext, device: Device, snapshot: dict) -> bool:
    """Confirm and reboot the device, then give control to the reconnect dialog of the session.

    After the command is sent, MeshTerm declares the link down
    (:meth:`~meshterm.context.AppContext.announce_reboot`), and does not wait for it.
    Thus the disconnect watcher of the session opens its dialog immediately, with a reboot
    label. The watcher waits for the companion to come back, and connects again. This is
    exactly the flow for an unplugged device, and nothing else is sent to the board while
    it restarts.

    Returns:
        ``True`` if the reboot was sent and the page must close. ``False`` if the user
        cancelled (or the simulator, which has no link to drop, absorbed the reboot).
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
        # The simulator has no link to drop, and it comes back immediately. Only send the
        # command.
        await device.reboot()
        ctx.ui.note("[warn]device rebooting[/warn]")
        await ctx.ui.present(title="Reboot")
        return False

    await device.reboot()
    # The command is sent, so the link is effectively down. Declare it, and do not wait for
    # a liveness poll to find it. If MeshTerm waits for a poll, a quick reboot behind a port
    # that never disappears is never found, and the next command goes to a board in the
    # middle of its restart.
    ctx.announce_reboot()
    # The watcher of the session now cancels this task and opens the reconnect dialog.
    # Wait here until it does, so that the page does not go back to the menu for a short
    # time between the two. The timeout is important only where no watcher runs to take
    # control.
    await asyncio.sleep(_REBOOT_HANDOFF_TIMEOUT_S)
    return True


async def _sync_clock(ctx: AppContext, device: Device, snapshot: dict) -> None:
    """Show the drift of the device clock against this computer, and offer to correct it.

    A companion that boots with a bad RTC puts a wrong time stamp on each message. Thus
    the dialog starts with the measured drift (or says that the clock cannot be read),
    before the Sync button writes the time of the host.
    """
    import time

    device_time: int | None = None
    try:
        device_time = await device.get_time()
    except Exception:  # noqa: BLE001 - old firmware. Offer the blind sync instead.
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
    """A device timestamp, rendered in the local time of this computer."""
    from datetime import datetime

    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _drift_text(drift: int) -> str:
    """Describe a clock drift in seconds: ``"12 s behind"``, ``"3 s ahead"``, ``"in sync"``."""
    if abs(drift) < 2:
        return "in sync with this computer"
    direction = "ahead of" if drift > 0 else "behind"
    return f"{abs(drift)} s {direction} this computer"


async def _backup_now(ctx: AppContext, device: Device, snapshot: dict) -> None:
    """Ask for a destination, and write the TOML backup immediately."""
    path = await ctx.ui.path(
        "Back up config",
        prompt="Write every setting to this TOML file:",
        default="meshterm-config.toml",
    )
    if path:
        await _run_now(ctx, device, snapshot, [("backup", Path(path))], "Backup")


async def _restore_now(ctx: AppContext, device: Device, snapshot: dict) -> bool:
    """Restore from a TOML backup: select the file, preview it if the user wants, then apply.

    Returns:
        ``True`` if the device changed (then the caller refreshes its snapshot).
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
    """Export or import the private identity key of the device.

    Returns:
        ``True`` if the identity changed (a key was imported). Then the caller reads its
        snapshot again.
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

    # Import: get the key, then ask for the typed confirmation.
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
    """Do a factory reset of the device, behind a typed confirmation.

    Returns:
        ``True`` if the reset ran. Then the caller discards all its staged values, and
        reads the device again.
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
    """Check that ``text`` is a hex string."""
    try:
        bytes.fromhex(text)
        return True
    except ValueError:
        return "Enter hex characters only."
