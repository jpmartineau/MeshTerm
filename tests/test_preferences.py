# SPDX-License-Identifier: Apache-2.0
"""Tests for MeshTerm's own preferences: the registry, the TOML file, and the page.

Three layers, in that order — what a preference *is* (spec, default, validation), where it
is kept (the file, and its tolerance for a hand edit gone wrong), and how it is changed
(the staged page, its reset row, and the gate on the way out) — plus the wiring checks that
keep a preference from being a value the page writes and nothing reads.
"""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.core.preferences import (
    GROUPS,
    PREFERENCES,
    PreferenceError,
    Preferences,
    by_group,
    format_value,
    get_spec,
    install,
    parse_value,
)
from meshterm.core.watch_store import WatchStore
from meshterm.persistence.repository import Repository
from meshterm.platforms import get_platform
from meshterm.ui.preferences import _menu_items, edit_preferences, preferences_table
from meshterm.ui.theme import active_theme
from tests.conftest import plain as _plain

# -- the registry ----------------------------------------------------------------


def test_every_preference_declares_a_usable_spec() -> None:
    """Keys are unique, groups are real, and every default survives its own validation."""
    keys = [spec.key for spec in PREFERENCES]
    assert len(keys) == len(set(keys))
    for spec in PREFERENCES:
        assert spec.group in GROUPS, f"{spec.key} is in an unlisted group"
        assert spec.help and spec.label, f"{spec.key} has no label or help"
        # The default is the value used unless overridden, so it has to be a value the
        # spec would accept from the file or the CLI.
        assert parse_value(spec, spec.default) == spec.default


def test_by_group_covers_every_preference_in_group_order() -> None:
    """The page's grouping is the whole registry, in the declared group order."""
    grouped = by_group()
    assert [group for group, _ in grouped] == [g for g in GROUPS if g in dict(grouped)]
    assert sum(len(specs) for _, specs in grouped) == len(PREFERENCES)


def test_defaults_are_what_an_untouched_install_reads() -> None:
    """With nothing overridden, every attribute reads its spec's default."""
    prefs = Preferences()
    for spec in PREFERENCES:
        assert getattr(prefs, spec.key) == spec.default
        assert not prefs.is_overridden(spec.key)
    assert prefs.overrides() == {}


def test_an_unknown_key_fails_where_it_is_written() -> None:
    """A mistyped preference raises rather than quietly reading nothing."""
    prefs = Preferences()
    with pytest.raises(AttributeError):
        prefs.trace_cooldwn_s  # noqa: B018 - the typo is the point
    with pytest.raises(PreferenceError):
        prefs.get("no_such_preference")


@pytest.mark.parametrize(
    "key,raw,expected",
    [
        ("fast_render", "off", False),
        ("fast_render", "yes", True),
        ("trace_cooldown_s", "2.5", 2.5),
        ("history_days", "0", 0),
        ("watch_silence_hours", "6", 6),  # an enum named as text lands on its own value
        ("full_width", "yes", "yes"),
    ],
)
def test_values_parse_from_text(key: str, raw: str, expected: Any) -> None:
    """Text — from the CLI, the file, or a typed prompt — coerces to the spec's type."""
    assert parse_value(get_spec(key), raw) == expected


@pytest.mark.parametrize(
    "key,raw",
    [
        ("direct_message_soft_retries", "9"),  # over the maximum
        ("map_view_fraction", "0"),  # under the minimum
        ("trace_cooldown_s", "soon"),  # not a number at all
        ("watch_silence_hours", "7"),  # not one of the silence choices
        ("fast_render", "maybe"),
    ],
)
def test_bad_values_are_refused_with_a_readable_message(key: str, raw: str) -> None:
    """Validation failures carry the key and the reason — the text the prompt shows."""
    with pytest.raises(PreferenceError) as exc:
        parse_value(get_spec(key), raw)
    assert key in str(exc.value)


def test_values_format_for_the_lane_they_are_drawn_in() -> None:
    """Booleans read as words, enums as their labels, numbers carry their unit."""
    assert format_value(get_spec("fast_render"), True) == "on"
    assert format_value(get_spec("fast_render"), False) == "off"
    assert format_value(get_spec("watch_silence_hours"), 6) == "6 h"
    assert format_value(get_spec("watch_silence_hours"), 0) == "off"
    assert format_value(get_spec("trace_cooldown_s"), 2.5) == "2.5 s"
    assert format_value(get_spec("history_days"), 365) == "365 days"


# -- the file --------------------------------------------------------------------


def test_only_a_disagreement_with_the_default_is_recorded() -> None:
    """Setting a value back to its default clears the override rather than pinning it."""
    prefs = Preferences()
    prefs.set("history_days", 30)
    assert prefs.overrides() == {"history_days": 30}

    prefs.set("history_days", get_spec("history_days").default)
    assert prefs.overrides() == {}
    assert prefs.history_days == 365


def test_the_file_round_trips_and_holds_only_the_overrides(tmp_path: Path) -> None:
    """What is saved is what was changed; everything else comes back from the code."""
    path = tmp_path / "preferences.toml"
    prefs = Preferences(path)
    prefs.set("trace_cooldown_s", 2.5)
    prefs.set("fast_render", False)
    prefs.save()

    text = path.read_text(encoding="utf-8")
    assert "trace_cooldown_s = 2.5" in text
    assert "fast_render = false" in text
    assert "history_days" not in text  # untouched, so not written

    reloaded = Preferences.load(path)
    assert reloaded.trace_cooldown_s == 2.5
    assert reloaded.fast_render is False
    assert reloaded.history_days == 365  # the code's default, not a stale copy


def test_the_file_reads_the_way_the_page_does(tmp_path: Path) -> None:
    """Grouped under comment headings, each entry above its help and its default."""
    prefs = Preferences(tmp_path / "preferences.toml")
    prefs.set("history_days", 30)
    text = prefs.as_toml()
    assert "# --- History ---" in text
    assert "# Older ones are deleted; 0 keeps everything" in text
    assert "# default: 365 days" in text
    # A group with nothing changed in it earns no heading.
    assert "# --- Display ---" not in text


def test_an_untouched_file_says_so(tmp_path: Path) -> None:
    """Saving with nothing overridden writes a file that explains its own emptiness."""
    prefs = Preferences(tmp_path / "preferences.toml")
    prefs.save()
    assert "every preference is at its default" in (tmp_path / "preferences.toml").read_text(
        encoding="utf-8"
    )


def test_a_yes_no_choice_is_understood_however_it_is_typed(tmp_path: Path) -> None:
    """A bare ``yes`` is not TOML and ``false`` is the wrong type; the edit is understood."""
    path = tmp_path / "preferences.toml"
    path.write_text("full_width = yes\n", encoding="utf-8")
    assert Preferences.load(path).full_width == "yes"

    path.write_text("full_width = false\n", encoding="utf-8")
    assert Preferences.load(path).full_width == "no"

    # And what we write ourselves is quoted, so it round-trips as the string it is.
    prefs = Preferences(path)
    prefs.set("full_width", "yes")
    prefs.save()
    assert 'full_width = "yes"' in path.read_text(encoding="utf-8")
    assert Preferences.load(path).full_width == "yes"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not a mapping at all",
        "history_days = [1, 2, 3]\n",  # a container where a scalar belongs
        "no_such_preference = 3\n",  # a key the registry never had
        "history_days = yesterday\n",  # right key, unparseable value
        "{{{ not toml",  # not even a document
    ],
)
def test_a_broken_file_costs_the_line_not_the_session(tmp_path: Path, text: str) -> None:
    """A hand edit gone wrong falls back to defaults instead of refusing to start."""
    path = tmp_path / "preferences.toml"
    path.write_text(text, encoding="utf-8")
    assert Preferences.load(path).history_days == 365


def test_a_good_line_survives_a_bad_one(tmp_path: Path) -> None:
    """One unusable entry is dropped; the rest of the file still applies."""
    path = tmp_path / "preferences.toml"
    path.write_text("history_days = soon\ntrace_cooldown_s = 3.0\n", encoding="utf-8")
    prefs = Preferences.load(path)
    assert prefs.history_days == 365 and prefs.trace_cooldown_s == 3.0


def test_a_word_left_unquoted_is_read_as_the_text_it_is(tmp_path: Path) -> None:
    """The likeliest hand edit of all costs nothing; a truly broken line costs only itself."""
    path = tmp_path / "preferences.toml"
    path.write_text(
        'log_level = DEBUG  # louder\ntrace_cooldown_s = "3.0\nfast_render = false\n',
        encoding="utf-8",
    )
    prefs = Preferences.load(path)
    assert prefs.log_level == "DEBUG"
    assert prefs.trace_cooldown_s == get_spec("trace_cooldown_s").default  # unclosed quote
    assert prefs.fast_render is False  # TOML-valid, so it keeps its type


def test_a_missing_file_is_simply_the_defaults(tmp_path: Path) -> None:
    """Nothing has to exist for the app to run: absence *is* the default state."""
    prefs = Preferences.load(tmp_path / "never-written.toml")
    assert prefs.overrides() == {} and prefs.fast_render is True


def test_reset_drops_every_override() -> None:
    """Reset returns the whole set to the code's defaults and reports how many it undid."""
    prefs = Preferences()
    prefs.set("history_days", 30)
    prefs.set("trace_cooldown_s", 2.5)
    assert prefs.reset() == 2
    assert prefs.overrides() == {} and prefs.history_days == 365


def test_an_in_memory_set_refuses_to_save() -> None:
    """A set with no file behind it says so rather than silently dropping the write."""
    with pytest.raises(RuntimeError):
        Preferences().save()


# -- the page --------------------------------------------------------------------


#: Wide enough that no lane is truncated, so an assertion about a row is about the row and
#: not about the width it was read at. The platform widths are the gallery's job.
_WIDE = 100


def _rows(prefs: Preferences, pending: dict | None = None) -> tuple[str, list[str]]:
    """The page's title and the lines it draws, as the reader sees them."""
    from meshterm.ui.tui import SelectScreen

    title, items = _menu_items(prefs, pending or {})
    screen = SelectScreen(title, items)
    screen.note_viewport(len(items) + 4)  # no paging: every row on screen at once
    return title, [line.strip() for line in _plain(screen.render_body(_WIDE)).splitlines()]


def test_the_page_groups_every_preference_under_its_own_heading() -> None:
    """Each group is a section, and each of its preferences a row under it.

    "Its preferences" is what this platform is offered: a spec gated to another flavour
    (``PrefSpec.platforms``) has no row here, which is the gate's whole visible effect and
    is pinned in ``tests/test_vt_console_font.py``.
    """
    _, items = _menu_items(Preferences(), {})
    _, rows = _rows(Preferences())
    values = [item.value for item in items if hasattr(item, "value")]
    for group, specs in by_group(get_platform().name):
        assert any(f"── {group} ──" in row for row in rows), f"{group} has no heading"
        for spec in specs:
            assert spec.key in values, f"{spec.key} has no row"
            assert any(spec.label in row for row in rows), f"{spec.label} is not drawn"


def test_a_clean_page_offers_neither_apply_nor_reset() -> None:
    """Nothing to save and nothing to undo means neither row is drawn (Esc just leaves)."""
    title, rows = _rows(Preferences())
    assert title == "Preferences"
    assert not any("Apply" in row for row in rows)
    assert not any("Reset to defaults" in row for row in rows)
    assert not any("Defaults" in row for row in rows)


def test_a_changed_preference_brings_out_the_reset_row() -> None:
    """The reset row appears once there is something for it to undo, and counts it."""
    prefs = Preferences()
    prefs.set("history_days", 30)
    _, rows = _rows(prefs)
    assert any("Reset to defaults…" in row for row in rows)
    assert any("Return the one changed value" in row for row in rows)


def test_a_staged_change_shows_its_arrow_and_the_save_action() -> None:
    """A staged row reads ``current → new``, and the Apply/Back pair appears below."""
    title, rows = _rows(Preferences(), {"history_days": 90})
    assert title == "Preferences — 1 staged"
    assert any("365 days → 90 days" in row for row in rows)
    assert any("✓ Apply 1 staged change" in row for row in rows)
    assert any("✗ Back — discard staged changes" in row for row in rows)


def test_a_long_value_is_capped_so_the_descriptions_keep_their_lane() -> None:
    """The basemap URL is shortened in the lane rather than eating the prose column."""
    _, rows = _rows(Preferences())
    basemap = next(row for row in rows if "Map tiles" in row)
    assert "https://tiles.openf…" in basemap
    assert "Where map images" in basemap  # its description still made it onto the row


def _table_lines(prefs: Preferences, width: int) -> list[str]:
    """``preferences show``'s output at ``width`` columns."""
    console = Console(width=width, file=io.StringIO(), theme=active_theme(), legacy_windows=False)
    with console.capture() as capture:
        console.print(preferences_table(prefs, width))
    return _plain(capture.get()).splitlines()


def test_a_description_scrolls_under_a_pinned_setting_and_value() -> None:
    """A row too wide to read slides its DESCRIPTION lane with ←→; the lanes left of it stay."""
    from meshterm.ui.tui import SelectScreen

    title, items = _menu_items(Preferences(), {})
    screen = SelectScreen(title, items)
    screen.note_viewport(len(items) + 4)
    before = _plain(screen.render_body(72)).splitlines()
    cursor = next(line for line in before if line.lstrip().startswith("❯"))
    assert cursor.rstrip().endswith("…")  # the description runs past the edge

    # ←→ is advertised exactly where it would act (SelectScreen folds the atom in itself).
    assert "←→ scroll" in screen.footer_hint

    for _ in range(4):
        screen.handle("right")
    scrolled = next(
        line
        for line in _plain(screen.render_body(72)).splitlines()
        if line.lstrip().startswith("❯")
    )
    # The setting and its value are the row's identity and have not moved; only the
    # explanation slid, and it now carries the mark saying there is more to its left.
    head = cursor.split("  ")[0]
    assert scrolled.startswith(head)
    assert "…" in scrolled and scrolled != cursor


def test_the_printed_table_names_every_value_and_its_default() -> None:
    """``preferences show`` prints the full value, the default beside it, and marks changes."""
    prefs = Preferences()
    prefs.set("history_days", 30)
    body = "\n".join(_table_lines(prefs, 160))
    assert "https://tiles.openfreemap.org/planet" in body  # never capped here
    assert "30 days" in body and "365 days" in body  # value beside its default
    assert "── History ──" in body
    assert "Older ones are deleted" in body  # the description lane, at this width


def test_the_printed_table_never_elides_a_key() -> None:
    """The key is what ``preferences set`` takes, so a narrow console shortens prose instead."""
    lines = _table_lines(Preferences(), 79)
    assert any("direct_message_soft_retries" in line for line in lines)
    assert not any("DESCRIPTION" in line for line in lines)  # the lane that gave way


# -- driving the page ------------------------------------------------------------


class _FakeVisit:
    """One round of a visited screen: the script's next ``select`` answer."""

    def __init__(self, ui: _ScriptedUi) -> None:
        self._ui = ui

    async def result(self) -> Any:
        from meshterm.ui.tui.screen import CANCEL

        value = self._ui._answer("select")
        return CANCEL if value is None else value


class _FakeSession:
    """Enough of :class:`TuiSession` for the page's ``stay`` loop."""

    def __init__(self, ui: _ScriptedUi) -> None:
        self.ui = ui
        self.pushed: list = []

    @asynccontextmanager
    async def stay(self, screen: Any) -> Any:
        self.pushed.append(screen)
        yield _FakeVisit(self.ui)


class _ScriptedUi:
    """A fake UI surface answering every prompt from a FIFO ``(method, answer)`` script."""

    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.script = list(script)
        self.session = _FakeSession(self)

    def _answer(self, method: str) -> Any:
        assert self.script, f"unexpected prompt: {method}"
        expected, value = self.script.pop(0)
        assert expected == method, f"expected {expected} prompt, got {method}"
        return value

    async def select(self, title: str, items: list, **kwargs: Any) -> Any:
        return self._answer("select")

    async def dialog(self, prompt: str, buttons: list, **kwargs: Any) -> Any:
        return self._answer("dialog")

    async def text(self, title: str, **kwargs: Any) -> str | None:
        return self._answer("text")

    def show(self, *renderables: Any) -> None:
        pass

    def note(self, markup: str) -> None:
        pass

    def ack(self, markup: str) -> None:
        pass


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """A mock-backed application context with its own preferences file."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "prefs.db")
    context = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


def _install(ctx: AppContext, script: list[tuple[str, Any]]) -> _ScriptedUi:
    ui = _ScriptedUi(script)
    ctx.ui = ui  # type: ignore[assignment]
    return ui


async def test_the_page_stages_a_typed_value_and_apply_returns_it(ctx: AppContext) -> None:
    """Editing a row stages it; the Apply row hands the map to the tool to write."""
    _install(
        ctx,
        [
            ("select", "history_days"),
            ("text", "30"),
            ("select", "__apply__"),
        ],
    )
    assert await edit_preferences(ctx) == {"history_days": 30}
    # Nothing reached disk: the page stages, the tool saves.
    assert not (ctx.settings.config_dir / "preferences.toml").exists()


async def test_the_page_unstages_a_value_set_back_to_where_it_started(ctx: AppContext) -> None:
    """Typing the value already in force clears the row instead of staging a no-op."""
    _install(
        ctx,
        [
            ("select", "history_days"),
            ("text", "30"),
            ("select", "history_days"),
            ("text", "365"),  # back to what is in force
            ("select", None),  # nothing staged — Esc leaves with no discard dialog
        ],
    )
    assert await edit_preferences(ctx) is None


async def test_leaving_with_unsaved_changes_is_gated(ctx: AppContext) -> None:
    """Esc with something staged asks first; "keep editing" returns to the same page."""
    _install(
        ctx,
        [
            ("select", "history_days"),
            ("text", "30"),
            ("select", None),  # Esc
            ("dialog", "keep"),  # ... and think better of it
            ("select", "__apply__"),
        ],
    )
    assert await edit_preferences(ctx) == {"history_days": 30}


def _preview_console(monkeypatch: pytest.MonkeyPatch, *, on_device: bool) -> list[str]:
    """Stand in for the console-font service: record every font the page loads."""
    from meshterm.ui import preferences as page

    loaded: list[str] = []
    monkeypatch.setattr(page.consolefont, "applies", lambda: on_device)
    monkeypatch.setattr(page.consolefont, "apply", lambda choice: loaded.append(choice) or True)
    return loaded


async def test_picking_a_console_font_previews_it_and_apply_keeps_it(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console loads the font the moment it is picked; Apply hands it over to save."""
    loaded = _preview_console(monkeypatch, on_device=True)
    _install(ctx, [("select", "console_font"), ("select", "6x8"), ("select", "__apply__")])
    assert await edit_preferences(ctx) == {"console_font": "6x8"}
    assert loaded == ["6x8"]  # previewed once, and nothing put back on the way out


async def test_discarding_a_previewed_console_font_puts_the_saved_one_back(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview that is not kept is undone: the saved font comes back on discard."""
    loaded = _preview_console(monkeypatch, on_device=True)
    _install(
        ctx,
        [
            ("select", "console_font"),
            ("select", "6x8"),
            ("select", None),  # Esc
            ("dialog", "discard"),
        ],
    )
    assert await edit_preferences(ctx) is None
    assert loaded == ["6x8", "6x12"]


async def test_setting_the_console_font_back_reloads_it_at_once(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging the value in force again is a clean row — and the console follows it."""
    loaded = _preview_console(monkeypatch, on_device=True)
    _install(
        ctx,
        [
            ("select", "console_font"),
            ("select", "6x8"),
            ("select", "console_font"),
            ("select", "6x12"),  # back to what is saved: nothing staged, Esc leaves quietly
            ("select", None),
        ],
    )
    assert await edit_preferences(ctx) is None
    assert loaded == ["6x8", "6x12"]


async def test_a_desktop_page_never_touches_a_console_font(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off the handheld the service says no once, and the page asks it nothing more."""
    loaded = _preview_console(monkeypatch, on_device=False)
    _install(ctx, [("select", "console_font"), ("select", "6x8"), ("select", "__apply__")])
    assert await edit_preferences(ctx) == {"console_font": "6x8"}
    assert loaded == []


async def test_discarding_at_the_gate_drops_the_changes(ctx: AppContext) -> None:
    """Confirming the discard leaves with nothing, staged values and all."""
    _install(
        ctx,
        [
            ("select", "history_days"),
            ("text", "30"),
            ("select", None),  # Esc
            ("dialog", "discard"),
        ],
    )
    assert await edit_preferences(ctx) is None


async def test_the_back_row_runs_the_same_gate_as_esc(ctx: AppContext) -> None:
    """``✗ Back — discard staged changes`` is Esc's twin, confirm and all."""
    _install(
        ctx,
        [
            ("select", "history_days"),
            ("text", "30"),
            ("select", "__cancel__"),
            ("dialog", "discard"),
        ],
    )
    assert await edit_preferences(ctx) is None


async def test_reset_stages_the_defaults_rather_than_writing_them(ctx: AppContext) -> None:
    """Reset is a staged change like any other: confirmed, reviewable, and discardable."""
    ctx.preferences.set("history_days", 30)
    ctx.preferences.set("trace_cooldown_s", 2.5)
    _install(
        ctx,
        [
            ("select", "__reset__"),
            ("dialog", True),
            ("select", "__apply__"),
        ],
    )
    assert await edit_preferences(ctx) == {"history_days": 365, "trace_cooldown_s": 5.0}
    # Still only staged — the values in force are untouched until the tool applies them.
    assert ctx.preferences.history_days == 30


async def test_cancelling_the_reset_confirm_stages_nothing(ctx: AppContext) -> None:
    """Backing out of the confirm leaves the page exactly as it was."""
    ctx.preferences.set("history_days", 30)
    _install(
        ctx,
        [
            ("select", "__reset__"),
            ("dialog", False),
            ("select", None),  # nothing staged, so Esc leaves without a discard dialog
        ],
    )
    assert await edit_preferences(ctx) is None


# -- the tool, and what actually reads a preference ------------------------------


async def test_the_tool_writes_the_staged_values_once(ctx: AppContext) -> None:
    """Applying the page's ops saves the file, and the saved file reads back the same."""
    from meshterm.tools.base import get_tool

    _install(ctx, [])
    tool = get_tool("preferences")
    ops = [("set", "history_days", 30), ("set", "full_width", "yes")]
    result = await tool.run(ctx, {"ops": ops})
    assert result.summary == {"changes": 2}

    path = ctx.settings.config_dir / "preferences.toml"
    assert path.exists()
    assert Preferences.load(path).history_days == 30


async def test_the_tool_reports_a_bad_value_rather_than_writing_it(ctx: AppContext) -> None:
    """A refused value never reaches the file."""
    _install(ctx, [])
    from meshterm.tools.base import get_tool

    with pytest.raises(PreferenceError):
        await get_tool("preferences").run(ctx, {"ops": [("set", "history_days", "soon")]})
    assert not (ctx.settings.config_dir / "preferences.toml").exists()


def test_the_context_installs_its_preferences_process_wide(ctx: AppContext) -> None:
    """The render layer, which has no context, still reads this session's values."""
    from meshterm.core.preferences import current

    assert current() is ctx.preferences


def test_a_newly_watched_node_takes_the_preferred_silence_rule(tmp_path: Path) -> None:
    """The Watchtower's silence preference is what a star actually arms the alarm with."""
    prefs = Preferences()
    prefs.set("watch_silence_hours", 3)
    install(prefs)
    try:
        store = WatchStore(tmp_path / "watchtower.json")
        store.watch("a1b2c3d4e5f6", "Alice")
        assert store.watched()["a1b2c3d4e5f6"].silence_hours == 3
    finally:
        install(Preferences())


def test_the_last_column_preference_overrules_the_platform() -> None:
    """``full_width`` steps aside on ``auto`` and decides otherwise (env still wins over both)."""
    from meshterm.platforms import PICOCALC, set_platform
    from meshterm.ui.tui.session import _reclaim_last_column

    set_platform(PICOCALC)  # a platform that defaults the reclaim off
    prefs = Preferences()
    install(prefs)
    try:
        assert _reclaim_last_column() is False  # auto: the platform's verdict stands
        prefs.set("full_width", "yes")
        assert _reclaim_last_column() is True
        prefs.set("full_width", "no")
        assert _reclaim_last_column() is False
    finally:
        install(Preferences())


# -- the weekly advert's status line ---------------------------------------------------


def _advert_rows(prefs: Preferences, pending: dict, status) -> list[str]:
    """The page's lines with a stub scheduler status."""
    from meshterm.ui.tui import SelectScreen

    title, items = _menu_items(prefs, pending, lambda: status)
    screen = SelectScreen(title, items)
    screen.note_viewport(len(items) + 4)
    return [line.strip() for line in _plain(screen.render_body(_WIDE)).splitlines()]


def test_no_status_line_while_the_weekly_advert_is_off() -> None:
    """Off by default, the row stands alone."""
    rows = _advert_rows(Preferences(), {}, None)
    assert not any("advert in" in row or "due —" in row for row in rows)


def test_staged_on_says_the_week_starts_on_apply() -> None:
    """Before it is saved, no week is running yet."""
    rows = _advert_rows(Preferences(), {"weekly_flood_advert": True}, None)
    assert "the week starts on Apply" in rows


def test_the_status_line_reads_the_scheduler() -> None:
    """Counting shows the time left; due says what the advert is waiting for."""
    from datetime import timedelta

    from meshterm.core.models import utcnow
    from meshterm.services.advert_scheduler import (
        COUNTING,
        LISTENING,
        OFFLINE,
        QUIET,
        AdvertStatus,
    )

    prefs = Preferences()
    prefs.set("weekly_flood_advert", True)
    soon = AdvertStatus(COUNTING, utcnow() + timedelta(days=4, hours=2))
    assert "next advert in 4d" in _advert_rows(prefs, {}, soon)
    assert "no device connected" in _advert_rows(prefs, {}, AdvertStatus(OFFLINE))
    assert "due — waiting for a packet" in _advert_rows(prefs, {}, AdvertStatus(LISTENING))
    assert "due — after 30 s of quiet" in _advert_rows(prefs, {}, AdvertStatus(QUIET))


def test_the_status_line_sits_under_its_row() -> None:
    """The line follows the Weekly advert row directly, so it reads as that row's."""
    from meshterm.services.advert_scheduler import QUIET, AdvertStatus

    prefs = Preferences()
    prefs.set("weekly_flood_advert", True)
    rows = _advert_rows(prefs, {}, AdvertStatus(QUIET))
    at = next(i for i, row in enumerate(rows) if row.startswith("Weekly advert"))
    assert rows[at + 1].startswith("due —")
