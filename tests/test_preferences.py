# SPDX-License-Identifier: Apache-2.0
"""Tests for the preferences of MeshTerm: the registry, the TOML file, and the page.

The tests have three layers, in this order:

* What a preference *is*: the spec, the default, and the validation.
* Where MeshTerm stores a preference: the file, and how it tolerates a hand edit that is
  not correct.
* How the user changes a preference: the staged page, its reset row, and the gate when
  the user leaves.

There are also checks of the wiring. These make sure that a preference is not a value that
the page writes and that nothing reads.
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
    """The keys are unique, the groups are real, and each default passes its own validation."""
    keys = [spec.key for spec in PREFERENCES]
    assert len(keys) == len(set(keys))
    for spec in PREFERENCES:
        assert spec.group in GROUPS, f"{spec.key} is in an unlisted group"
        assert spec.help and spec.label, f"{spec.key} has no label or help"
        # MeshTerm uses the default unless an override exists, so the default must be a
        # value that the spec accepts from the file or the CLI.
        assert parse_value(spec, spec.default) == spec.default


def test_by_group_covers_every_preference_in_group_order() -> None:
    """The grouping of the page is the whole registry.

    The groups are in the order that they were declared.
    """
    grouped = by_group()
    assert [group for group, _ in grouped] == [g for g in GROUPS if g in dict(grouped)]
    assert sum(len(specs) for _, specs in grouped) == len(PREFERENCES)


def test_defaults_are_what_an_untouched_install_reads() -> None:
    """If nothing is overridden, each attribute reads the default of its spec."""
    prefs = Preferences()
    for spec in PREFERENCES:
        assert getattr(prefs, spec.key) == spec.default
        assert not prefs.is_overridden(spec.key)
    assert prefs.overrides() == {}


def test_an_unknown_key_fails_where_it_is_written() -> None:
    """A preference with a typing error raises an error.

    It does not read nothing, without a message.
    """
    prefs = Preferences()
    with pytest.raises(AttributeError):
        prefs.trace_cooldwn_s  # noqa: B018 - the typing error is the purpose of the test
    with pytest.raises(PreferenceError):
        prefs.get("no_such_preference")


@pytest.mark.parametrize(
    "key,raw,expected",
    [
        ("fast_render", "off", False),
        ("fast_render", "yes", True),
        ("trace_cooldown_s", "2.5", 2.5),
        ("history_days", "0", 0),
        ("watch_silence_hours", "6", 6),  # an enum that is written as text gives its own value
        ("full_width", "yes", "yes"),
    ],
)
def test_values_parse_from_text(key: str, raw: str, expected: Any) -> None:
    """Text (from the CLI, the file, or a prompt) becomes the type of the spec."""
    assert parse_value(get_spec(key), raw) == expected


@pytest.mark.parametrize(
    "key,raw",
    [
        ("direct_message_soft_retries", "9"),  # more than the maximum
        ("map_view_fraction", "0"),  # less than the minimum
        ("trace_cooldown_s", "soon"),  # not a number
        ("watch_silence_hours", "7"),  # not one of the silence choices
        ("fast_render", "maybe"),
    ],
)
def test_bad_values_are_refused_with_a_readable_message(key: str, raw: str) -> None:
    """A validation failure has the key and the reason. This is the text that the prompt shows."""
    with pytest.raises(PreferenceError) as exc:
        parse_value(get_spec(key), raw)
    assert key in str(exc.value)


def test_values_format_for_the_lane_they_are_drawn_in() -> None:
    """Booleans are words, enums are their labels, and numbers have their unit."""
    assert format_value(get_spec("fast_render"), True) == "on"
    assert format_value(get_spec("fast_render"), False) == "off"
    assert format_value(get_spec("watch_silence_hours"), 6) == "6 h"
    assert format_value(get_spec("watch_silence_hours"), 0) == "off"
    assert format_value(get_spec("trace_cooldown_s"), 2.5) == "2.5 s"
    assert format_value(get_spec("history_days"), 365) == "365 days"


# -- the file --------------------------------------------------------------------


def test_only_a_disagreement_with_the_default_is_recorded() -> None:
    """If the user sets a value back to its default, MeshTerm clears the override.

    It does not keep it.
    """
    prefs = Preferences()
    prefs.set("history_days", 30)
    assert prefs.overrides() == {"history_days": 30}

    prefs.set("history_days", get_spec("history_days").default)
    assert prefs.overrides() == {}
    assert prefs.history_days == 365


def test_the_file_round_trips_and_holds_only_the_overrides(tmp_path: Path) -> None:
    """The file has what the user changed. MeshTerm gets everything else from the code."""
    path = tmp_path / "preferences.toml"
    prefs = Preferences(path)
    prefs.set("trace_cooldown_s", 2.5)
    prefs.set("fast_render", False)
    prefs.save()

    text = path.read_text(encoding="utf-8")
    assert "trace_cooldown_s = 2.5" in text
    assert "fast_render = false" in text
    assert "history_days" not in text  # not changed, so MeshTerm does not write it

    reloaded = Preferences.load(path)
    assert reloaded.trace_cooldown_s == 2.5
    assert reloaded.fast_render is False
    assert reloaded.history_days == 365  # the default from the code, not an old copy


def test_the_file_reads_the_way_the_page_does(tmp_path: Path) -> None:
    """The file is in groups under comment headings.

    Each entry has its help and its default above it.
    """
    prefs = Preferences(tmp_path / "preferences.toml")
    prefs.set("history_days", 30)
    text = prefs.as_toml()
    assert "# --- History ---" in text
    assert "# Older ones are deleted; 0 keeps everything" in text
    assert "# default: 365 days" in text
    # A group with no change in it has no heading.
    assert "# --- Display ---" not in text


def test_an_untouched_file_says_so(tmp_path: Path) -> None:
    """A save with no override writes a file that explains why it is empty."""
    prefs = Preferences(tmp_path / "preferences.toml")
    prefs.save()
    assert "every preference is at its default" in (tmp_path / "preferences.toml").read_text(
        encoding="utf-8"
    )


def test_a_yes_no_choice_is_understood_however_it_is_typed(tmp_path: Path) -> None:
    """A bare ``yes`` is not TOML, and ``false`` is the wrong type.

    MeshTerm understands the edit in each case.
    """
    path = tmp_path / "preferences.toml"
    path.write_text("full_width = yes\n", encoding="utf-8")
    assert Preferences.load(path).full_width == "yes"

    path.write_text("full_width = false\n", encoding="utf-8")
    assert Preferences.load(path).full_width == "no"

    # MeshTerm quotes what it writes. Thus the value is read again as the string that it is.
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
        "history_days = [1, 2, 3]\n",  # a container where a scalar is necessary
        "no_such_preference = 3\n",  # a key that the registry never had
        "history_days = yesterday\n",  # a correct key, a value that the parser cannot read
        "{{{ not toml",  # not a document
    ],
)
def test_a_broken_file_costs_the_line_not_the_session(tmp_path: Path, text: str) -> None:
    """A hand edit that is not correct gives the defaults. MeshTerm does not refuse to start."""
    path = tmp_path / "preferences.toml"
    path.write_text(text, encoding="utf-8")
    assert Preferences.load(path).history_days == 365


def test_a_good_line_survives_a_bad_one(tmp_path: Path) -> None:
    """MeshTerm removes one entry that it cannot use. The rest of the file still applies."""
    path = tmp_path / "preferences.toml"
    path.write_text("history_days = soon\ntrace_cooldown_s = 3.0\n", encoding="utf-8")
    prefs = Preferences.load(path)
    assert prefs.history_days == 365 and prefs.trace_cooldown_s == 3.0


def test_a_word_left_unquoted_is_read_as_the_text_it_is(tmp_path: Path) -> None:
    """The most probable hand edit has no cost. A line that is truly broken costs only itself."""
    path = tmp_path / "preferences.toml"
    path.write_text(
        'log_level = DEBUG  # louder\ntrace_cooldown_s = "3.0\nfast_render = false\n',
        encoding="utf-8",
    )
    prefs = Preferences.load(path)
    assert prefs.log_level == "DEBUG"
    assert prefs.trace_cooldown_s == get_spec("trace_cooldown_s").default  # unclosed quote
    assert prefs.fast_render is False  # valid TOML, so it keeps its type


def test_a_missing_file_is_simply_the_defaults(tmp_path: Path) -> None:
    """The app does not need the file to run. If the file is absent, this *is* the default state."""
    prefs = Preferences.load(tmp_path / "never-written.toml")
    assert prefs.overrides() == {} and prefs.fast_render is True


def test_reset_drops_every_override() -> None:
    """Reset returns the whole set to the defaults of the code.

    It returns the number of overrides that it removed.
    """
    prefs = Preferences()
    prefs.set("history_days", 30)
    prefs.set("trace_cooldown_s", 2.5)
    assert prefs.reset() == 2
    assert prefs.overrides() == {} and prefs.history_days == 365


def test_an_in_memory_set_refuses_to_save() -> None:
    """A set with no file says this with an error.

    It does not remove the write without a message.
    """
    with pytest.raises(RuntimeError):
        Preferences().save()


# -- the page --------------------------------------------------------------------


#: A width that is large enough that no lane is truncated. Thus an assertion about a row is
#: about the row, and not about the width where the test read it. The gallery tests the
#: widths of the platforms.
_WIDE = 100


def _rows(prefs: Preferences, pending: dict | None = None) -> tuple[str, list[str]]:
    """The title of the page and the lines that it draws, as the user sees them."""
    from meshterm.ui.tui import SelectScreen

    title, items = _menu_items(prefs, pending or {})
    screen = SelectScreen(title, items)
    screen.note_viewport(len(items) + 4)  # no paging: each row is visible at the same time
    return title, [line.strip() for line in _plain(screen.render_body(_WIDE)).splitlines()]


def test_the_page_groups_every_preference_under_its_own_heading() -> None:
    """Each group is a section, and each of its preferences is a row under it.

    "Its preferences" are the preferences that this platform has. A spec that is limited to
    another platform (``PrefSpec.platforms``) has no row here. This is the whole visible
    effect of the limit, and ``tests/test_vt_console_font.py`` tests it.
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
    """If there is nothing to save and nothing to undo, the page draws neither row.

    Esc leaves the page.
    """
    title, rows = _rows(Preferences())
    assert title == "Preferences"
    assert not any("Apply" in row for row in rows)
    assert not any("Reset to defaults" in row for row in rows)
    assert not any("Defaults" in row for row in rows)


def test_a_changed_preference_brings_out_the_reset_row() -> None:
    """The reset row appears when it has something to undo, and it shows the count."""
    prefs = Preferences()
    prefs.set("history_days", 30)
    _, rows = _rows(prefs)
    assert any("Reset to defaults…" in row for row in rows)
    assert any("Return the one changed value" in row for row in rows)


def test_a_staged_change_shows_its_arrow_and_the_save_action() -> None:
    """A staged row shows ``current → new``, and the Apply and Back pair appears below it."""
    title, rows = _rows(Preferences(), {"history_days": 90})
    assert title == "Preferences — 1 staged"
    assert any("365 days → 90 days" in row for row in rows)
    assert any("✓ Apply 1 staged change" in row for row in rows)
    assert any("✗ Back — discard staged changes" in row for row in rows)


def test_a_long_value_is_capped_so_the_descriptions_keep_their_lane() -> None:
    """The page makes the basemap URL shorter in the lane.

    It does not let the URL use the prose column.
    """
    _, rows = _rows(Preferences())
    basemap = next(row for row in rows if "Map tiles" in row)
    assert "https://tiles.openf…" in basemap
    assert "Where map images" in basemap  # its description is still on the row


def _table_lines(prefs: Preferences, width: int) -> list[str]:
    """The output of ``preferences show`` at ``width`` columns."""
    console = Console(width=width, file=io.StringIO(), theme=active_theme(), legacy_windows=False)
    with console.capture() as capture:
        console.print(preferences_table(prefs, width))
    return _plain(capture.get()).splitlines()


def test_a_description_scrolls_under_a_pinned_setting_and_value() -> None:
    """A row that is too wide slides its DESCRIPTION lane with ←→.

    The lanes to the left of it stay.
    """
    from meshterm.ui.tui import SelectScreen

    title, items = _menu_items(Preferences(), {})
    screen = SelectScreen(title, items)
    screen.note_viewport(len(items) + 4)
    before = _plain(screen.render_body(72)).splitlines()
    cursor = next(line for line in before if line.lstrip().startswith("❯"))
    assert cursor.rstrip().endswith("…")  # the description goes past the edge

    # The hint shows ←→ exactly where the keys act (SelectScreen adds the atom itself).
    assert "←→ scroll" in screen.footer_hint

    for _ in range(4):
        screen.handle("right")
    scrolled = next(
        line
        for line in _plain(screen.render_body(72)).splitlines()
        if line.lstrip().startswith("❯")
    )
    # The setting and its value are the identity of the row, and they did not move. Only the
    # explanation slid, and it now has the mark that shows that there is more to its left.
    head = cursor.split("  ")[0]
    assert scrolled.startswith(head)
    assert "…" in scrolled and scrolled != cursor


def test_the_printed_table_names_every_value_and_its_default() -> None:
    """``preferences show`` prints the full value and the default beside it.

    It also marks the changes.
    """
    prefs = Preferences()
    prefs.set("history_days", 30)
    body = "\n".join(_table_lines(prefs, 160))
    assert "https://tiles.openfreemap.org/planet" in body  # the table never caps it
    assert "30 days" in body and "365 days" in body  # the value beside its default
    assert "── History ──" in body
    assert "Older ones are deleted" in body  # the description lane, at this width


def test_the_printed_table_never_elides_a_key() -> None:
    """The key is what ``preferences set`` takes.

    Thus a narrow console makes the prose shorter instead.
    """
    lines = _table_lines(Preferences(), 79)
    assert any("direct_message_soft_retries" in line for line in lines)
    assert not any("DESCRIPTION" in line for line in lines)  # the lane that is removed


# -- running the page ------------------------------------------------------------


class _FakeVisit:
    """One round of a screen that stays: the next ``select`` answer of the script."""

    def __init__(self, ui: _ScriptedUi) -> None:
        self._ui = ui

    async def result(self) -> Any:
        from meshterm.ui.tui.screen import CANCEL

        value = self._ui._answer("select")
        return CANCEL if value is None else value


class _FakeSession:
    """The part of :class:`TuiSession` that the ``stay`` loop of the page needs."""

    def __init__(self, ui: _ScriptedUi) -> None:
        self.ui = ui
        self.pushed: list = []

    @asynccontextmanager
    async def stay(self, screen: Any) -> Any:
        self.pushed.append(screen)
        yield _FakeVisit(self.ui)


class _ScriptedUi:
    """A fake UI surface.

    It answers each prompt from a FIFO script of ``(method, answer)`` pairs.
    """

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
    """An application context with a mock, and with its own preferences file."""
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
    """If the user edits a row, the page stages it.

    The Apply row gives the map to the tool, which writes it.
    """
    _install(
        ctx,
        [
            ("select", "history_days"),
            ("text", "30"),
            ("select", "__apply__"),
        ],
    )
    assert await edit_preferences(ctx) == {"history_days": 30}
    # Nothing reached the disk. The page stages, and the tool saves.
    assert not (ctx.settings.config_dir / "preferences.toml").exists()


async def test_the_page_unstages_a_value_set_back_to_where_it_started(ctx: AppContext) -> None:
    """If the user types the value that is in force already, the page clears the row.

    It does not stage a change that does nothing.
    """
    _install(
        ctx,
        [
            ("select", "history_days"),
            ("text", "30"),
            ("select", "history_days"),
            ("text", "365"),  # back to the value in force
            ("select", None),  # nothing is staged: Esc leaves with no discard dialog
        ],
    )
    assert await edit_preferences(ctx) is None


async def test_leaving_with_unsaved_changes_is_gated(ctx: AppContext) -> None:
    """If a change is staged, Esc asks first. "Keep editing" returns to the same page."""
    _install(
        ctx,
        [
            ("select", "history_days"),
            ("text", "30"),
            ("select", None),  # Esc
            ("dialog", "keep"),  # the user changes the decision
            ("select", "__apply__"),
        ],
    )
    assert await edit_preferences(ctx) == {"history_days": 30}


def _preview_console(monkeypatch: pytest.MonkeyPatch, *, on_device: bool) -> list[str]:
    """A stand-in for the console-font service. It records each font that the page loads."""
    from meshterm.ui import preferences as page

    loaded: list[str] = []
    monkeypatch.setattr(page.consolefont, "applies", lambda: on_device)
    monkeypatch.setattr(page.consolefont, "apply", lambda choice: loaded.append(choice) or True)
    return loaded


async def test_picking_a_console_font_previews_it_and_apply_keeps_it(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console loads the font when the user selects it.

    Apply gives the font to the tool to save.
    """
    loaded = _preview_console(monkeypatch, on_device=True)
    _install(ctx, [("select", "console_font"), ("select", "6x8"), ("select", "__apply__")])
    assert await edit_preferences(ctx) == {"console_font": "6x8"}
    assert loaded == ["6x8"]  # one preview, and nothing is put back when the page closes


async def test_discarding_a_previewed_console_font_puts_the_saved_one_back(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MeshTerm undoes a preview that the user does not keep.

    The saved font comes back at the discard.
    """
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
    """If the user stages the value in force again, the row is clean, and the console follows it."""
    loaded = _preview_console(monkeypatch, on_device=True)
    _install(
        ctx,
        [
            ("select", "console_font"),
            ("select", "6x8"),
            ("select", "console_font"),
            ("select", "6x12"),  # back to the saved value: nothing is staged, and Esc leaves
            ("select", None),
        ],
    )
    assert await edit_preferences(ctx) is None
    assert loaded == ["6x8", "6x12"]


async def test_a_desktop_page_never_touches_a_console_font(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a desktop, the service says no one time, and the page asks it nothing more."""
    loaded = _preview_console(monkeypatch, on_device=False)
    _install(ctx, [("select", "console_font"), ("select", "6x8"), ("select", "__apply__")])
    assert await edit_preferences(ctx) == {"console_font": "6x8"}
    assert loaded == []


async def test_discarding_at_the_gate_drops_the_changes(ctx: AppContext) -> None:
    """If the user confirms the discard, the page leaves with nothing.

    The staged values are removed.
    """
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
    """``✗ Back — discard staged changes`` does the same as Esc, with the confirm."""
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
    """Reset is a staged change like each other change.

    It has a confirm, the user can examine it, and the user can discard it.
    """
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
    # The change is only staged. The values in force do not change until the tool applies them.
    assert ctx.preferences.history_days == 30


async def test_cancelling_the_reset_confirm_stages_nothing(ctx: AppContext) -> None:
    """If the user goes back from the confirm, the page does not change."""
    ctx.preferences.set("history_days", 30)
    _install(
        ctx,
        [
            ("select", "__reset__"),
            ("dialog", False),
            ("select", None),  # nothing is staged, so Esc leaves with no discard dialog
        ],
    )
    assert await edit_preferences(ctx) is None


# -- the tool, and the code that reads a preference ------------------------------


async def test_the_tool_writes_the_staged_values_once(ctx: AppContext) -> None:
    """If the tool applies the ops of the page, it saves the file.

    The saved file has the same values when MeshTerm reads it.
    """
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
    """A value that the tool refuses never goes to the file."""
    _install(ctx, [])
    from meshterm.tools.base import get_tool

    with pytest.raises(PreferenceError):
        await get_tool("preferences").run(ctx, {"ops": [("set", "history_days", "soon")]})
    assert not (ctx.settings.config_dir / "preferences.toml").exists()


def test_the_context_installs_its_preferences_process_wide(ctx: AppContext) -> None:
    """The render layer has no context, but it still reads the values of this session."""
    from meshterm.core.preferences import current

    assert current() is ctx.preferences


def test_a_newly_watched_node_takes_the_preferred_silence_rule(tmp_path: Path) -> None:
    """The silence preference of the Watchtower is the rule that a star uses to arm the alarm."""
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
    """``full_width`` has no effect on ``auto``, and it decides in the other cases.

    The environment still has priority over both.
    """
    from meshterm.platforms import PICOCALC_LYRA, set_platform
    from meshterm.ui.tui.session import _reclaim_last_column

    set_platform(PICOCALC_LYRA)  # a platform where the reclaim is off by default
    prefs = Preferences()
    install(prefs)
    try:
        assert _reclaim_last_column() is False  # auto: the decision of the platform stays
        prefs.set("full_width", "yes")
        assert _reclaim_last_column() is True
        prefs.set("full_width", "no")
        assert _reclaim_last_column() is False
    finally:
        install(Preferences())


# -- the status line of the weekly advert ----------------------------------------------


def _advert_rows(prefs: Preferences, pending: dict, status) -> list[str]:
    """The lines of the page, with a scheduler status that is a stub."""
    from meshterm.ui.tui import SelectScreen

    title, items = _menu_items(prefs, pending, lambda: status)
    screen = SelectScreen(title, items)
    screen.note_viewport(len(items) + 4)
    return [line.strip() for line in _plain(screen.render_body(_WIDE)).splitlines()]


def test_no_status_line_while_the_weekly_advert_is_off() -> None:
    """The weekly advert is off by default, and the row has no status line."""
    rows = _advert_rows(Preferences(), {}, None)
    assert not any("advert in" in row or "due —" in row for row in rows)


def test_staged_on_says_the_week_starts_on_apply() -> None:
    """Before the user saves it, no week is in progress."""
    rows = _advert_rows(Preferences(), {"weekly_flood_advert": True}, None)
    assert "the week starts on Apply" in rows


def test_the_status_line_reads_the_scheduler() -> None:
    """The status line shows the time that is left.

    When the advert is due, it shows what the advert waits for.
    """
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
    """The line is directly after the Weekly advert row. Thus it looks like part of that row."""
    from meshterm.services.advert_scheduler import QUIET, AdvertStatus

    prefs = Preferences()
    prefs.set("weekly_flood_advert", True)
    rows = _advert_rows(prefs, {}, AdvertStatus(QUIET))
    at = next(i for i, row in enumerate(rows) if row.startswith("Weekly advert"))
    assert rows[at + 1].startswith("due —")
