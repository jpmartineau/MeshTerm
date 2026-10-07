# SPDX-License-Identifier: Apache-2.0
"""The Preferences page: MeshTerm's own behaviour, staged and saved.

The app-side twin of the device-configuration editor (:mod:`meshterm.ui.config_editor`).
It uses the same parts, on purpose:

- One grouped list with aligned lanes.
- A row that stages ``current → new`` in the warn style.
- An Apply/Back pair that appears only when something is staged.
- The shared discard confirm when the user leaves.

Two editors that stage values must not be two different screens for the user to learn.
Thus only two things are different here. These are what the page edits
(:mod:`meshterm.core.preferences`, not the radio) and where Apply writes
(``preferences.toml``, not the companion).

Its rows scroll sideways, but the rows of the device editor do not. The explanation of a
preference is the widest part of its row, and it is the only part that goes past the edge.
Thus ←→ slide the DESCRIPTION lane under a setting and a value that stay pinned. Without
this, the description is lost at the fold.

The page also has a **Defaults** section. It has one row that stages each preference back
to its built-in default. It follows the same rule as the Apply/Back pair: the page draws it
only when there is something to reset, because a row that offers to undo nothing does
nothing. A reset *stages* the values. It does not write. Thus the user can examine the
reset, and discard it with Esc, as with each other change on the page. The one action at the
bottom is the only action that writes the file.

:func:`preferences_table` is the same content as a printed table, for ``preferences show``
of the CLI.
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

#: The preference whose row has a live status line under it (refer to :func:`_advert_line`).
_WEEKLY_ADVERT = "weekly_flood_advert"

#: The sentinels of the menu actions. They are different from the plain-string preference
#: keys that the rows have.
_RESET = "__reset__"
_APPLY = "__apply__"
_CANCEL = "__cancel__"


async def edit_preferences(ctx: AppContext) -> dict[str, Any] | None:
    """Run the Preferences editor and return the values to save.

    This function writes nothing. It returns the staged map to
    :class:`~meshterm.tools.preferences.PreferencesTool`, which applies it and logs it. The
    device-config editor divides the work in the same way with its own tool.

    Args:
        ctx: The shared application context (it gives the preferences and the UI surface).

    Returns:
        The staged ``key -> value`` map to write. It is ``None`` if the user left with
        nothing staged, or chose to discard what was staged.
    """
    from .tui import CANCEL, SelectScreen

    session = getattr(ctx.ui, "session", None)
    if session is None:  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the preferences editor is only available in the menu")
    prefs = ctx.preferences
    pending: dict[str, Any] = {}  # preference key -> staged new value

    # The console font previews. A VT loads one font for the whole screen. Thus the only
    # accurate way to show the 6x8 build is to load it, and this page is the preview: it
    # paints again with the new row count as soon as the user selects the value.
    # ``shown`` is the font that the console uses now. A discard puts the saved value back,
    # and the re-apply of Apply after the write finds nothing to change.
    previewing = consolefont.applies()
    shown = prefs.get(consolefont.PREFERENCE_KEY)

    def preview() -> None:
        nonlocal shown
        if not previewing:
            return
        want = _effective(prefs, consolefont.PREFERENCE_KEY, pending)
        if want != shown and consolefont.apply(want):
            shown = want

    # The status line of the weekly advert reads the scheduler at each paint. Ask the
    # scheduler to read the store again now, so that the first paint does not report a week
    # from before the last Apply.
    ctx.adverts.recheck()
    advert_status = ctx.adverts.status

    # The rows are the data. Each row has its own staged ``current → new`` value, and the
    # title counts what is staged. Thus the rows refresh in place after each round
    # (``replace_items``), and the code does not build them again as a new screen. One
    # screen for the whole visit means that the typed filter stays when the user edits a
    # preference, and not only the highlight.
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
                    continue  # keep editing: the same list, in the same place
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
    """The value that ``key`` has if the code applies the staged changes now."""
    return pending[key] if key in pending else prefs.get(key)


def _resettable(prefs: Preferences, pending: dict[str, Any]) -> list[str]:
    """The keys whose effective value is different from their default. A reset changes these.

    Staged values count. Thus a preference that the user edited in this visit can be reset
    before it is saved. A preference that is staged back to its own default cannot be reset.
    """
    return [
        spec.key for spec in _all_specs() if _effective(prefs, spec.key, pending) != spec.default
    ]


def _page_groups() -> list[tuple[str, list[PrefSpec]]]:
    """The groups that this page draws: all the preferences that the running platform offers.

    A preference that is limited to another platform (the console font of the PicoCalc, on
    a desktop) is a row whose question this machine cannot answer, so the page does not draw
    it. Its value is still in the registry, and it still goes through the file and back.
    Thus a desktop does not quietly empty a ``preferences.toml`` that a handheld wrote.
    """
    return by_group(get_platform().name)


def _all_specs() -> list[PrefSpec]:
    """All the specs that the page draws, in page order (the groups in one flat list)."""
    return [spec for _, specs in _page_groups() for spec in specs]


#: The cap in cells on each side of the ``current → staged`` of a VALUE lane. The lane has
#: the width of its widest row, and the description lane gets the cells that are left.
#: Without a cap, one long value (the basemap URL, at 36 cells) would use a third of the
#: screen, and the prose of each other row would become an ellipsis. With the cap, only that
#: row is shortened, and the row still opens a prompt that shows the whole value.
_VALUE_CAP = 20


def _capped(text: str) -> Text:
    """One side of a VALUE lane, shortened to :data:`_VALUE_CAP` with an ellipsis."""
    value = Text(text)
    value.truncate(_VALUE_CAP, overflow="ellipsis")
    return value


def _preference_value(prefs: Preferences, spec: PrefSpec, pending: dict[str, Any]) -> Text:
    """The VALUE lane of one preference: ``current [→ staged]``.

    The page draws the staged arrow in the warn style, so that a changed row is easy to see.
    The span stays visible on the highlighted row, because the highlight tints only the base
    style of the row. The two sides have separate caps. Thus a staged change never hides
    behind the value that it replaces.
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
    """Build the title and the rows of the page for the current values and the staged values.

    The rows are in three aligned lanes under one pinned header: the preference, the current
    value (and the staged new value, if there is one), and the description. Thus the page
    looks like the editor that it is.

    ``advert_status`` is :meth:`~meshterm.services.advert_scheduler.AdvertScheduler.status`
    of the scheduler. While the weekly advert is on (or staged on), its row gets a muted
    line under it that says where the week is (refer to :func:`_advert_line`).
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

    # The page draws the reset row only when a reset does something. The reason is the same
    # as for the Apply/Back pair, which the page draws only when something is staged (refer
    # to menus.exit_rows). If a control undoes nothing, the user must try it to learn that.
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

    # The shared editor header (menus.lane_header), pinned for the whole list. The lanes
    # have the same meaning in each group. Thus a row that scrolls keeps both its column
    # header and its section heading above it.
    items: list = [Separator(lambda w: lane_header(label_w, value_w, w), pinned=True)]
    # All the lanes before the DESCRIPTION lane are the identity of the row (which
    # preference it is, and its value), and they always fit. Only the explanation goes past
    # the edge. Thus ←→ slide the explanation under a setting and a value that stay pinned
    # (Choice.hscroll_from, the same arrangement that menu_rows gives a command list).
    # Without this, the descriptions were lost at the fold. On the 53 columns of the
    # PicoCalc, this is most of each description.
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
                # A user cannot trust a timer that the user cannot see, so say where the
                # week is. The line is a callable. Thus each paint reads the scheduler
                # again, and the line counts down (and changes to "due") while the page is
                # open. It is in the value lane, under the "on" that it explains. The page
                # draws it, also when it is blank, for as long as the preference is on.
                # Thus the rows below never move when the reading arrives. A separator has
                # no "❯ " pointer column, so it adds those two cells itself.
                indent = 2 + label_w + 2
                items.append(
                    Separator(
                        lambda width, indent=indent: _advert_line(
                            prefs, pending, advert_status, indent, width
                        )
                    )
                )

    # While the page is clean, there is nothing here, and Esc leaves. When changes are
    # staged, the pair appears below one blank line. Apply is the save action, and Back
    # shows what it costs to leave instead (refer to menus.exit_rows).
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
    """The status of the weekly advert, muted, in the value lane, and fitted to ``width``.

    If the preference is staged on but not saved, the week has not started, and the line
    says when it will start. In other cases the function reads the scheduler. The line shows
    the time until the advert of the connected device is due. When the advert is due, the
    line shows what the advert waits for. The line is blank when there is nothing to report.
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


#: The widest terminal that still cannot hold the DESCRIPTION lane. The table must have
#: three lanes: the key that the user types into ``preferences set``, the value that the
#: preference has, and the value that it has if the user did not change it. These lanes take
#: approximately eighty cells together. The descriptions are the lane that gives way, as
#: they do in the device-config table on a narrow console. They are still on the page, and
#: in the comments of the file itself.
_DESCRIBE_FROM = 100


def preferences_table(prefs: Preferences, width: int = _DESCRIBE_FROM) -> Table:
    """Build the full preferences table: each value, its default, and what it does.

    This is the printed face of the page, for ``meshterm preferences show``. Unlike the
    screen, it lists the default of each preference beside its value. A terminal print has
    no row to open, and no reset row from which to infer the difference. It also names each
    preference by its **key**, because ``preferences set`` takes the key.

    "Each" means each, platform limits included (``by_group()`` with no argument). A user
    often types a command line on one machine about a file that belongs to another machine.
    If the listing hides a key, nobody would think to ``set`` it.

    Args:
        prefs: The preferences to render.
        width: The width of the console. It decides whether the page draws the DESCRIPTION
            lane.

    Returns:
        A Rich :class:`Table` that the caller can pass to ``ctx.ui.show``.
    """
    # The same frameless SIMPLE_HEAD shape as the device-config table, so that the two
    # printed settings views look like one family.
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
    # The key lane has the width of the longest key, so that the table never elides a key.
    # Nobody can type back a truncated `direct_message_s…`. The two value lanes wrap
    # instead (the basemap URL is 36 cells on its own), and DESCRIPTION takes the rest.
    keys_w = max(len(spec.key) for spec in PREFERENCES) + 2
    table.add_column("PREFERENCE", style="muted", no_wrap=True, min_width=keys_w)
    table.add_column("VALUE")
    table.add_column("DEFAULT", style="muted")
    if describe:
        table.add_column("DESCRIPTION", style="muted")

    def row(cells: list[str]) -> list[str]:
        """Trim a row to the lanes that this width draws."""
        return cells if describe else cells[:-1]

    for group, specs in by_group():
        table.add_section()
        table.add_row(*row([f"[accent]── {group} ──[/accent]", "", "", ""]))
        for spec in specs:
            value = format_value(spec, prefs.get(spec.key))
            # An overridden value is the only part of the row that needs a colour. It is the
            # value on which this install does not agree with the code.
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
    """Prompt for the new value of one preference and stage it.

    If the user stages the value that is already in force, nothing changes. Thus a row that
    is set back to its start value is clean again, and the page does not count it as a
    change that changes nothing.
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
    """Prompt for a typed value for ``spec`` in the correct dialog. Return ``None`` on cancel."""
    if spec.value_type == "bool":
        # A choice between two states is clearest as a pair of buttons. The current state
        # is highlighted, so that Enter does not change anything by accident.
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
    """Confirm, then stage each changed preference back to its built-in default.

    The dialog is amber and not red. Nothing is written yet, and Esc still discards the
    reset. But one key press that undoes each preference that the user ever changed needs a
    question first.
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
        # The rule is the same as for a row that the user edits by hand. Stage the default
        # only where the saved value is not the default. Where it is, the entry was a staged
        # change, and the code removes it.
        if prefs.get(key) == default:
            pending.pop(key, None)
        else:
            pending[key] = default


__all__ = ["edit_preferences", "preferences_table"]
