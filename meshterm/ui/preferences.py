# SPDX-License-Identifier: Apache-2.0
"""The Preferences page: MeshTerm's own behaviour, staged and saved.

The app-side twin of the device-configuration editor (:mod:`meshterm.ui.config_editor`),
built out of the same parts on purpose — one grouped, lane-aligned list; a row that stages
``current → new`` in the warn style; an Apply/Back pair that appears only once something
is staged; the shared discard confirm on the way out. Two editors that stage values should
not be two different screens to learn, so the only things that differ here are what is
being edited (:mod:`meshterm.core.preferences`, not the radio) and where Apply writes
(``preferences.toml``, not the companion).

Its rows scroll sideways, which the device editor's do not: a preference's explanation is
the widest thing on its row and the only part that runs past the edge, so ←→ slide the
DESCRIPTION lane under a pinned setting and value rather than letting it die at the fold.

What the page adds is the **Defaults** section: one row that stages every preference back
to its built-in default. It follows the same rule the Apply/Back pair does — it is drawn
only while there is something to reset, because a row offering to undo nothing is a row
that does nothing. Resetting *stages*; it does not write. So the reset is as reviewable,
and as discardable with Esc, as any other change on the page, and the file is still only
ever written by the one action at the bottom.

:func:`preferences_table` is the same content as a printed table, for the CLI's
``preferences show``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich import box
from rich.cells import cell_len
from rich.table import Table
from rich.text import Text

from ..core.models import utcnow
from ..core.preferences import (
    PREFERENCES,
    PreferenceError,
    Preferences,
    PrefSpec,
    by_group,
    format_value,
    get_spec,
    parse_value,
    range_hint,
)
from ..platforms import get_platform
from ..services import consolefont
from .menus import (
    confirm_discard,
    exit_rows,
    lane_header,
    lane_row,
    section_heading,
)
from .tui import Choice, Separator

if TYPE_CHECKING:
    from ..context import AppContext
    from ..services.advert_scheduler import AdvertStatus

#: The preference whose row carries a live status line under it (see :func:`_advert_line`).
_WEEKLY_ADVERT = "weekly_flood_advert"

#: Menu action sentinels, distinct from the plain-string preference keys the rows carry.
_RESET = "__reset__"
_APPLY = "__apply__"
_CANCEL = "__cancel__"


async def edit_preferences(ctx: AppContext) -> dict[str, Any] | None:
    """Run the Preferences editor and return the values to save.

    Nothing is written here: the staged map goes back to
    :class:`~meshterm.tools.preferences.PreferencesTool` to apply and log, the same
    division of labour the device-config editor keeps with its own tool.

    Args:
        ctx: Shared application context (provides the preferences and the UI surface).

    Returns:
        The staged ``key -> value`` map to write, or ``None`` if the reader left with
        nothing staged (or chose to discard what was).
    """
    from .tui import CANCEL, SelectScreen

    session = getattr(ctx.ui, "session", None)
    if session is None:  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the preferences editor is only available in the menu")
    prefs = ctx.preferences
    pending: dict[str, Any] = {}  # preference key -> staged new value

    # The console font previews. A VT loads one font for the whole screen, so the only
    # honest way to show what the 6x8 build looks like is to load it — and this page is
    # the preview: it repaints at the new row count the moment the value is picked.
    # ``shown`` is what the console is drawn in right now; a discard puts the saved value
    # back, and Apply's own re-apply after the write finds nothing to change.
    previewing = consolefont.applies()
    shown = prefs.get(consolefont.PREFERENCE_KEY)

    def preview() -> None:
        nonlocal shown
        if not previewing:
            return
        want = _effective(prefs, consolefont.PREFERENCE_KEY, pending)
        if want != shown and consolefont.apply(want):
            shown = want

    # The weekly advert's status line reads the scheduler on every paint; ask it to re-read
    # the store now, so the first paint does not report a week from before the last Apply.
    ctx.adverts.recheck()
    advert_status = ctx.adverts.status

    # The rows *are* the data — each carries its own staged ``current → new`` value and
    # the title counts what is staged — so they refresh in place after every round
    # (``replace_items``) rather than being rebuilt as a new screen. One screen for the
    # whole visit means the typed filter survives editing a preference, not just the cursor.
    title, items = _menu_items(prefs, pending, advert_status)
    menu = SelectScreen(
        title,
        items,
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
    )
    async with session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            if choice is CANCEL:  # Esc at the list
                choice = _CANCEL

            if choice in (None, _CANCEL):
                if pending and not await confirm_discard(ctx, len(pending), verb="saving"):
                    continue  # keep editing — the same list, the same place in it
                if previewing and shown != prefs.get(consolefont.PREFERENCE_KEY):
                    consolefont.apply(prefs.get(consolefont.PREFERENCE_KEY))
                return None
            if choice == _APPLY:
                return dict(pending) or None
            if choice == _RESET:
                await _stage_reset(ctx, prefs, pending)
            else:  # a preference key
                await _stage_preference(ctx, prefs, choice, pending)
            preview()
            title, items = _menu_items(prefs, pending, advert_status)
            menu.replace_items(items, title=title)


# --- rendering ----------------------------------------------------------------


def _effective(prefs: Preferences, key: str, pending: dict[str, Any]) -> Any:
    """What ``key`` would be worth if the staged changes were applied right now."""
    return pending[key] if key in pending else prefs.get(key)


def _resettable(prefs: Preferences, pending: dict[str, Any]) -> list[str]:
    """The keys whose *effective* value differs from their default — what a reset would touch.

    Staged values count, so a preference edited this visit is resettable before it is ever
    saved, and one staged back to its own default already isn't.
    """
    return [
        spec.key for spec in _all_specs() if _effective(prefs, spec.key, pending) != spec.default
    ]


def _page_groups() -> list[tuple[str, list[PrefSpec]]]:
    """The groups this page draws — everything the *running* platform is offered.

    A preference gated to another flavour (the PicoCalc's console font, on a desktop) is a
    row whose question this machine cannot answer, so it is not drawn. Its value is still
    in the registry and still round-trips through the file, which is what keeps a
    ``preferences.toml`` written on the handheld from being quietly emptied by a desktop.
    """
    return by_group(get_platform().name)


def _all_specs() -> list[PrefSpec]:
    """Every spec the page draws, in page order (the grouping flattened)."""
    return [spec for _, specs in _page_groups() for spec in specs]


#: Cell cap on *each side* of a VALUE lane's ``current → staged``. The lane is sized to
#: its widest row, and the description lane gets whatever is left — so one long value (the
#: basemap URL, at 36 cells) would otherwise spend a third of the screen on itself and
#: shred every other row's prose down to an ellipsis. Capped, only that row is shortened,
#: and the row still opens to a prompt showing the value in full.
_VALUE_CAP = 20


def _capped(text: str) -> Text:
    """One side of a VALUE lane, shortened to :data:`_VALUE_CAP` with an ellipsis."""
    value = Text(text)
    value.truncate(_VALUE_CAP, overflow="ellipsis")
    return value


def _preference_value(prefs: Preferences, spec: PrefSpec, pending: dict[str, Any]) -> Text:
    """One preference's VALUE lane: ``current [→ staged]``.

    The staged arrow is drawn in the warn style so a dirty row stands out at a glance; the
    span survives the select highlight, which only tints the row's base style. Both sides
    are capped independently, so a staged change never hides behind the value it replaces.
    """
    value = _capped(format_value(spec, prefs.get(spec.key)))
    if spec.key in pending:
        staged = _capped(format_value(spec, pending[spec.key]))
        value.append(" → ", style="warn")
        value.append_text(Text(staged.plain, style="warn"))
    return value


def _menu_items(
    prefs: Preferences,
    pending: dict[str, Any],
    advert_status: Callable[[], AdvertStatus | None] | None = None,
) -> tuple[str, list]:
    """Build the page's title and rows for the current values plus whatever is staged.

    The rows sit in three aligned lanes — preference, current value (and any staged new
    value), description — under one pinned header, so the page reads as the editor it is.

    ``advert_status`` is the scheduler's :meth:`~meshterm.services.advert_scheduler.
    AdvertScheduler.status`; while the weekly advert is on (or staged on) its row gets a
    muted line under it saying where the week stands (see :func:`_advert_line`).
    """
    sections: list[tuple[str, list[tuple[str, Text, str, Any]]]] = []
    for group, specs in _page_groups():
        sections.append(
            (
                group,
                [
                    (
                        spec.label,
                        _preference_value(prefs, spec, pending),
                        spec.description,
                        spec.key,
                    )
                    for spec in specs
                ],
            )
        )

    # The reset row is drawn only while a reset would do something, for the reason the
    # Apply/Back pair is drawn only while something is staged (see menus.exit_rows): an
    # affordance that undoes nothing is one the reader has to try to learn that.
    resettable = _resettable(prefs, pending)
    if resettable:
        count = len(resettable)
        sections.append(
            (
                "Defaults",
                [
                    (
                        "Reset to defaults…",
                        Text(),
                        f"Return {count} changed values to default"
                        if count > 1
                        else "Return the one changed value to default",
                        _RESET,
                    )
                ],
            )
        )

    label_w = max(cell_len(label) for _, rows in sections for label, _, _, _ in rows)
    value_w = max(cell_len(value.plain) for _, rows in sections for _, value, _, _ in rows)

    # The shared editor header (menus.lane_header), pinned for the whole list: the lanes
    # mean the same in every group, so a scrolled row keeps both its column header and its
    # section heading overhead.
    items: list = [Separator(lambda w: lane_header(label_w, value_w, w), pinned=True)]
    # Everything up to the DESCRIPTION lane is the row's identity — which preference, and
    # what it is set to — and it always fits. Only the explanation runs past the edge, so
    # ←→ slide *it* under a pinned setting and value (Choice.hscroll_from, the same
    # arrangement menu_rows gives a command list). Without it the descriptions simply died
    # at the fold, which on the PicoCalc's 53 columns is most of every one of them.
    description_at = label_w + 2 + value_w + 2
    for group, rows in sections:
        items.append(section_heading(group))
        for label, value, help_text, key in rows:
            items.append(
                Choice(
                    title=lane_row(label, value, help_text, label_w, value_w),
                    value=key,
                    hscroll_from=description_at,
                )
            )
            if key == _WEEKLY_ADVERT and _effective(prefs, key, pending):
                # A timer you cannot see is one you cannot trust: say where the week is. A
                # callable, so every paint reads the scheduler afresh and the line counts
                # down (and flips to "due") while the page is open. Set in the value lane,
                # under the "on" it explains; drawn — even blank — for as long as the
                # preference is on, so the rows below never move when the reading arrives.
                # A separator has no "❯ " pointer column, so it pays those two cells itself.
                indent = 2 + label_w + 2
                items.append(
                    Separator(
                        lambda width, indent=indent: _advert_line(
                            prefs, pending, advert_status, indent, width
                        )
                    )
                )

    # Nothing at all while the page is clean — Esc leaves. With changes staged the pair
    # appears below one blank line: Apply is the save action, and Back spells out what
    # leaving instead would cost (see menus.exit_rows).
    items.extend(exit_rows(len(pending), apply_value=_APPLY, back_value=_CANCEL))

    title = "Preferences" + (f" — {len(pending)} staged" if pending else "")
    return title, items


def _advert_line(
    prefs: Preferences,
    pending: dict[str, Any],
    advert_status: Callable[[], AdvertStatus | None] | None,
    indent: int,
    width: int,
) -> Text:
    """The weekly advert's status, muted and set in the value lane, fitted to ``width``.

    Staged on but not yet saved, the week has not started, and the line says when it will.
    Otherwise it reads the scheduler: how long until the connected device's advert is due,
    and once it is, what the advert is waiting for. Blank when there is nothing to report.
    """
    from ..services.advert_scheduler import COUNTING, LISTENING, OFFLINE, QUIET
    from .widgets import format_age

    if not prefs.weekly_flood_advert:
        words = "the week starts on Apply"
    else:
        status = advert_status() if advert_status is not None else None
        if status is None:
            words = ""
        elif status.state == OFFLINE:
            words = "no device connected"
        elif status.state == COUNTING and status.due_at is not None:
            left = (status.due_at - utcnow()).total_seconds()
            words = "next advert in " + (
                "under a minute" if left < 60 else format_age(max(0.0, left))
            )
        elif status.state == LISTENING:
            words = "due — waiting for a packet"
        elif status.state == QUIET:
            words = f"due — after {prefs.advert_quiet_s} s of quiet"
        else:
            words = ""
    line = Text(" " * indent + words, style="muted")
    line.truncate(max(0, width), overflow="ellipsis")
    return line


#: Widest terminal that still can't hold the DESCRIPTION lane. The three lanes the table
#: can't do without — the key you would type into ``preferences set``, what it is set to,
#: and what it would be if you hadn't — take about eighty cells between them, and the
#: descriptions are the lane that gives way, exactly as they do in the device-config table
#: on a narrow console. They are still on the page, and in the file's own comments.
_DESCRIBE_FROM = 100


def preferences_table(prefs: Preferences, width: int = _DESCRIBE_FROM) -> Table:
    """Build the full preferences table — every value, its default, and what it does.

    The printed face of the page, for ``meshterm preferences show``. Unlike the screen it
    lists *every* preference's default beside its value, because a terminal print has no
    row to open and no reset row to infer the difference from — and it names each
    preference by its **key**, since that is what ``preferences set`` takes.

    "Every" is meant literally, platform gates included (``by_group()`` with no argument):
    a command line is regularly typed on one machine about a file that belongs to another,
    and a key the listing hid is a key nobody would think to ``set``.

    Args:
        prefs: The preferences to render.
        width: The console's width, which decides whether the DESCRIPTION lane is drawn.

    Returns:
        A Rich :class:`Table` ready to hand to ``ctx.ui.show``.
    """
    # Same frameless SIMPLE_HEAD shape as the device-config table, so the two printed
    # settings views read as one family.
    table = Table(
        title="[accent]Preferences[/accent]",
        title_justify="left",
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        header_style="muted",
        expand=False,
        padding=(0, 2, 0, 0),
    )
    describe = width >= _DESCRIBE_FROM
    # The key lane is pinned to the longest key so no key is ever elided: a truncated
    # `direct_message_s…` is not a thing anyone can type back. The two value lanes wrap
    # instead (the basemap URL is 36 cells on its own), and DESCRIPTION takes the rest.
    keys_w = max(len(spec.key) for spec in PREFERENCES) + 2
    table.add_column("PREFERENCE", style="muted", no_wrap=True, min_width=keys_w)
    table.add_column("VALUE")
    table.add_column("DEFAULT", style="muted")
    if describe:
        table.add_column("DESCRIPTION", style="muted")

    def row(cells: list[str]) -> list[str]:
        """Trim a row to the lanes this width actually draws."""
        return cells if describe else cells[:-1]

    for group, specs in by_group():
        table.add_section()
        table.add_row(*row([f"[accent]── {group} ──[/accent]", "", "", ""]))
        for spec in specs:
            value = format_value(spec, prefs.get(spec.key))
            # An overridden value is the only thing on the row worth a colour: it is what
            # this install disagrees with the code about.
            table.add_row(
                *row(
                    [
                        f"  {spec.key}",
                        f"[warn]{value}[/warn]" if prefs.is_overridden(spec.key) else value,
                        format_value(spec, spec.default),
                        spec.description,
                    ]
                )
            )
    return table


# --- staging individual changes -----------------------------------------------


async def _stage_preference(
    ctx: AppContext, prefs: Preferences, key: str, pending: dict[str, Any]
) -> None:
    """Prompt for one preference's new value and stage it.

    Staging the value already in force is a no-op, so a row set back to where it started
    reads clean again rather than counting as a change that changes nothing.
    """
    spec = get_spec(key)
    value = await _prompt_value(ctx, spec, _effective(prefs, key, pending))
    if value is None:
        return
    if value == prefs.get(key):
        pending.pop(key, None)
    else:
        pending[key] = value


async def _prompt_value(ctx: AppContext, spec: PrefSpec, current: Any) -> Any:
    """Prompt for a typed value for ``spec`` in the fitting dialog; ``None`` on cancel."""
    if spec.value_type == "bool":
        # A straight two-state choice reads best as a button pair, with the current state
        # highlighted so Enter changes nothing by accident.
        return await ctx.ui.dialog(
            spec.description,
            [("Off", False), ("On", True)],
            title=spec.label,
            default=1 if current else 0,
            keys={"0": False, "1": True, "n": False, "y": True},
        )

    if spec.value_type == "enum" and spec.choices is not None:
        items = [
            Choice(
                title=f"{label}" + ("  (current)" if value == current else ""),
                value=value,
            )
            for value, label in spec.choices.items()
        ]
        return await ctx.ui.select(
            spec.label, items, prompt=f"{spec.description}:", default=current
        )

    def validate(text: str) -> bool | str:
        try:
            parse_value(spec, text)
            return True
        except PreferenceError as exc:
            return str(exc)

    raw = await ctx.ui.text(
        spec.label,
        prompt=f"{spec.description}:",
        default="" if current is None else str(current),
        validate=validate,
        help_text=range_hint(spec),
    )
    return None if raw is None else parse_value(spec, raw)


async def _stage_reset(ctx: AppContext, prefs: Preferences, pending: dict[str, Any]) -> None:
    """Confirm, then stage every changed preference back to its built-in default.

    Amber rather than red: nothing is written yet and Esc still discards it, but one press
    undoing every preference you have ever changed deserves to be asked about first.
    """
    keys = _resettable(prefs, pending)
    if not keys:  # pragma: no cover - the row is only drawn when there is something to reset
        return
    plural = "" if len(keys) == 1 else "s"
    if (
        await ctx.ui.dialog(
            f"Stage {len(keys)} preference{plural} back to their built-in defaults?",
            [("Cancel", False), ("Reset", True)],
            title="Reset to defaults",
            default=1,
            danger=True,
        )
        is not True
    ):
        return
    for key in keys:
        default = get_spec(key).default
        # Same rule as a hand-edited row: stage the default only where the *saved* value
        # isn't already it — where it is, the entry was a staged change and simply goes.
        if prefs.get(key) == default:
            pending.pop(key, None)
        else:
            pending[key] = default


__all__ = ["edit_preferences", "preferences_table"]
