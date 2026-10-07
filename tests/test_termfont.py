# SPDX-License-Identifier: Apache-2.0
"""Tests for the detection of the terminal font: the recommended list, the ladder, the verdict.

Each verdict runs against injected environments, temporary settings files, and injected
probes. Thus the suite gives the same answers on any machine. Two tests use the real
machine on purpose: the scan of the installed fonts and the probe of the console buffer.
Both tests assert only that the question is safe. They never assert what this machine
answers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshterm.platforms import CARDPUTER_ZERO, PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui.fontset import CARDPUTER_ZERO_CODEPOINTS, FONT_CODEPOINTS
from meshterm.ui.termfont import (
    CORE,
    FULL,
    NERD_FONT_GENERIC,
    NONE,
    UNKNOWN,
    RecommendedFont,
    _emoji_support,
    _is_classic_console,
    _powerline_support,
    _read_jsonc,
    _vscode_default_face,
    _vscode_face,
    _windows_terminal_face,
    detect_terminal_font,
    installed_recommended,
    match_recommended,
    normalize_face,
    powerline_support,
    primary_family,
)

# --- name matching ----------------------------------------------------------------


def test_primary_family_takes_the_first_of_a_css_list() -> None:
    """VS Code stores comma lists with optional quotes, and MeshTerm judges the first family."""
    assert primary_family("'Hack Nerd Font Mono', monospace") == "Hack Nerd Font Mono"
    assert primary_family('"Fira Code", Consolas') == "Fira Code"
    assert primary_family("Cascadia Mono") == "Cascadia Mono"


def test_normalize_face_folds_case_quotes_and_spacing() -> None:
    """The match uses a form in lower case, with single spaces and no quotes."""
    assert normalize_face("  'Hack  Nerd Font' ") == "hack nerd font"
    assert normalize_face('"MesloLGS NF"') == "meslolgs nf"


def test_match_recommended_knows_the_nerd_font_spellings() -> None:
    """The long name and the short suffix of version 3 match the same full entry."""
    assert match_recommended("Hack Nerd Font Mono").name == "Hack Nerd Font"
    assert match_recommended("Hack NFM").name == "Hack Nerd Font"
    assert match_recommended("MesloLGS NF").coverage == FULL  # the font of powerlevel10k


def test_match_recommended_orders_full_patches_before_core_families() -> None:
    """A full patched family matches before the core family that it comes from.

    ``JetBrainsMono Nerd Font`` must match its full entry, and never the plain
    ``JetBrains Mono`` core prefix. The first match wins, so the full entries are first.
    """
    assert match_recommended("JetBrainsMono Nerd Font Mono").coverage == FULL
    assert match_recommended("JetBrains Mono").coverage == CORE


def test_match_recommended_core_natives_and_strangers() -> None:
    """Stock coder fonts with native triangles give core. All other fonts give no match."""
    assert match_recommended("Fira Code").coverage == CORE
    assert match_recommended("Source Code Variable").coverage == CORE
    assert match_recommended("Consolas") is None
    assert match_recommended(None) is None


def test_match_recommended_generic_nerd_rule_catches_unlisted_patches() -> None:
    """Each family that has the Nerd Font branding has the full block patched in."""
    assert match_recommended("Comic Shanns Mono Nerd Font") is NERD_FONT_GENERIC
    assert match_recommended("Terminess NF") is NERD_FONT_GENERIC


# --- settings-file reading --------------------------------------------------------


def test_read_jsonc_survives_comments_and_trailing_commas(tmp_path: Path) -> None:
    """WT and VS Code both write JSON with comments, and the function accepts it."""
    path = tmp_path / "settings.json"
    path.write_text(
        "{\n"
        "  // the font\n"
        '  "editor.fontFamily": "Hack NFM", /* inline */\n'
        '  "url": "https://example.org//not-a-comment",\n'
        '  "list": [1, 2,],\n'
        "}\n",
        encoding="utf-8",
    )
    data = _read_jsonc(path)
    assert data == {
        "editor.fontFamily": "Hack NFM",
        "url": "https://example.org//not-a-comment",
        "list": [1, 2],
    }
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    assert _read_jsonc(broken) is None


def _wt_env(tmp_path: Path, settings: dict, guid: str = "{abc-123}") -> dict:
    """A fake Windows Terminal environment with a settings file that the function writes."""
    folder = tmp_path / "Microsoft" / "Windows Terminal"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return {"LOCALAPPDATA": str(tmp_path), "WT_SESSION": "s", "WT_PROFILE_ID": guid}


def test_windows_terminal_face_resolves_profile_then_defaults(tmp_path: Path) -> None:
    """The ``font.face`` of the session profile wins, and ``profiles.defaults`` is the fallback."""
    env = _wt_env(
        tmp_path,
        {
            "profiles": {
                "defaults": {"font": {"face": "Cascadia Code PL"}},
                "list": [{"guid": "{ABC-123}", "font": {"face": "Hack Nerd Font Mono"}}],
            },
        },
    )
    assert _windows_terminal_face(env) == "Hack Nerd Font Mono"  # the GUID is case-folded
    env2 = _wt_env(
        tmp_path,
        {
            "profiles": {
                "defaults": {"font": {"face": "Cascadia Code PL"}},
                "list": [{"guid": "{other}"}],
            },
        },
    )
    assert _windows_terminal_face(env2) == "Cascadia Code PL"


def test_windows_terminal_face_reads_legacy_and_defaults_to_cascadia(tmp_path: Path) -> None:
    """The old flat ``fontFace`` key still counts. If nothing is set, the built-in face is used."""
    env = _wt_env(
        tmp_path,
        {
            "profiles": {"list": [{"guid": "{abc-123}", "fontFace": "MesloLGS NF"}]},
        },
    )
    assert _windows_terminal_face(env) == "MesloLGS NF"
    env2 = _wt_env(tmp_path, {"profiles": {"list": [{"guid": "{abc-123}"}]}})
    assert _windows_terminal_face(env2) == "Cascadia Mono"
    assert _windows_terminal_face({"LOCALAPPDATA": str(tmp_path / "nowhere")}) == "Cascadia Mono"


def test_vscode_face_precedence_terminal_over_editor_workspace_over_user(
    tmp_path: Path,
) -> None:
    """MeshTerm reads the font settings of VS Code in the order of precedence.

    ``terminal.integrated.fontFamily`` wins over ``editor.fontFamily``. For each key, the
    workspace file wins over the user file. MeshTerm judges the first family of the value.
    """
    workspace = tmp_path / "repo"
    (workspace / ".vscode").mkdir(parents=True)
    (workspace / ".vscode" / "settings.json").write_text(
        '{"editor.fontFamily": "Consolas"}', encoding="utf-8"
    )
    appdata = tmp_path / "appdata"
    user = appdata / "Code" / "User"
    user.mkdir(parents=True)
    (user / "settings.json").write_text(
        '{"terminal.integrated.fontFamily": "\'Hack Nerd Font Mono\', monospace"}',
        encoding="utf-8",
    )
    env = {"APPDATA": str(appdata)}
    assert _vscode_face(env, workspace) == "Hack Nerd Font Mono"  # terminal.* wins
    (user / "settings.json").write_text("{}", encoding="utf-8")
    assert _vscode_face(env, workspace) == "Consolas"  # the editor fallback of the workspace
    (workspace / ".vscode" / "settings.json").write_text("{}", encoding="utf-8")
    assert _vscode_face(env, workspace) == _vscode_default_face()


# --- the ladder -------------------------------------------------------------------


def test_detect_terminal_font_ladder(tmp_path: Path) -> None:
    """MeshTerm identifies the terminal with a ladder of markers.

    The order is Windows Terminal, then VS Code, then a real conhost. A ``TERM`` that is
    set removes the conhost probe, because another emulator hosts the console.
    """
    wt = detect_terminal_font(_wt_env(tmp_path, {"profiles": {"list": []}}))
    assert wt is not None and wt.source == "windows-terminal"
    code = detect_terminal_font({"TERM_PROGRAM": "vscode", "APPDATA": str(tmp_path)}, cwd=tmp_path)
    assert code is not None and code.source == "vscode"
    con = detect_terminal_font({}, conhost_probe=lambda: "Consolas")
    assert con is not None and (con.face, con.source) == ("Consolas", "conhost")
    assert detect_terminal_font({"TERM": "xterm"}, conhost_probe=lambda: "Consolas") is None


def test_powerline_support_env_override_always_wins() -> None:
    """``MESHTERM_POWERLINE`` is the decision of the user: off, on/full, or core."""
    assert _powerline_support({"MESHTERM_POWERLINE": "0"}).level == NONE
    assert _powerline_support({"MESHTERM_POWERLINE": "off"}).level == NONE
    on = _powerline_support({"MESHTERM_POWERLINE": "1", "WT_SESSION": "s"})
    assert (on.level, on.source) == (FULL, "env")
    assert _powerline_support({"MESHTERM_POWERLINE": "core"}).level == CORE


def test_powerline_support_matched_font_sets_the_level(tmp_path: Path) -> None:
    """A recommended face that MeshTerm reads from the config of the terminal sets the coverage."""
    env = _wt_env(
        tmp_path,
        {
            "profiles": {"list": [{"guid": "{abc-123}", "font": {"face": "Hack Nerd Font Mono"}}]},
        },
    )
    verdict = _powerline_support(env)
    assert verdict.level == FULL
    assert verdict.source == "font:windows-terminal"
    assert verdict.matched is not None and verdict.matched.name == "Hack Nerd Font"


def test_powerline_support_renderer_fallback_and_honest_none(tmp_path: Path) -> None:
    """An unmatched face still gives core where the renderer can supply the glyphs.

    On Windows Terminal, the fallback is the bundled symbols. The same face on a bare
    conhost gives ``none``.
    """
    env = _wt_env(tmp_path, {"profiles": {"list": [{"guid": "{abc-123}"}]}})
    wt = _powerline_support(env)  # the face is Cascadia Mono, which gives no match
    assert (wt.level, wt.source) == (CORE, "renderer:windows-terminal")
    con = _powerline_support({}, conhost_probe=lambda: "Consolas")
    assert (con.level, con.source, con.face) == (NONE, "font:conhost", "Consolas")


def test_powerline_support_kitty_ssh_and_unknown() -> None:
    """A terminal that draws the glyphs gives core by its marker. All others stay unknown.

    An ssh session and a terminal that MeshTerm does not recognize give ``unknown``
    and not ``none``, because the font is on a display that MeshTerm cannot see.
    """
    kitty = _powerline_support({"TERM": "xterm-kitty"})
    assert (kitty.level, kitty.source) == (CORE, "renderer:kitty")
    ssh = _powerline_support({"TERM": "xterm", "SSH_CONNECTION": "1.2.3.4"})
    assert (ssh.level, ssh.source) == (UNKNOWN, "ssh")
    lost = _powerline_support({"TERM": "xterm-256color"})
    assert (lost.level, lost.source) == (UNKNOWN, "unknown")


def test_powerline_support_a_handheld_answers_from_its_own_font() -> None:
    """The font of a handheld draws the screen, so its inventory answers and no terminal does.

    The Terminus of the Cardputer and the console font of the PicoCalc both have the core
    chevrons and no rounded caps. A font without the chevrons gives ``none``, also when
    kitty started the app. The override still wins over the font, as it wins over each
    probe.
    """
    kitty = {"TERM": "xterm-kitty"}  # a terminal that gives core in all other cases
    cardputer = _powerline_support(kitty, handheld=("cardputer-zero", CARDPUTER_ZERO_CODEPOINTS))
    assert (cardputer.level, cardputer.source) == (CORE, "platform:cardputer-zero")
    picocalc = _powerline_support(kitty, handheld=("picocalc-lyra", FONT_CODEPOINTS))
    assert (picocalc.level, picocalc.source) == (CORE, "platform:picocalc-lyra")
    bare = _powerline_support(kitty, handheld=("bare", frozenset(range(0x20, 0x7F))))
    assert (bare.level, bare.source) == (NONE, "platform:bare")
    capped = CARDPUTER_ZERO_CODEPOINTS | {0xE0B4, 0xE0B6}
    assert _powerline_support({}, handheld=("capped", capped)).level == FULL
    pinned = {"MESHTERM_POWERLINE": "0"}
    assert _powerline_support(pinned, handheld=("cardputer-zero", CARDPUTER_ZERO_CODEPOINTS)) == (
        _powerline_support(pinned)
    )


def test_powerline_support_follows_a_platform_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform switch clears the cached verdict, so it never answers for the last display."""
    monkeypatch.delenv("MESHTERM_POWERLINE", raising=False)
    monkeypatch.setenv("TERM", "xterm-kitty")
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    set_platform(CARDPUTER_ZERO)
    assert powerline_support().source == "platform:cardputer-zero"
    set_platform(PICOCALC_LYRA)
    assert powerline_support().source == "platform:picocalc-lyra"
    set_platform(REGULAR)
    assert powerline_support().source == "renderer:kitty"


def test_installed_recommended_never_raises() -> None:
    """The scan of the machine for installed fonts never raises an exception.

    It gives information for a future screen of suggestions, and it does its best. For
    each answer of the platform, the result is a RecommendedFont or None.
    """
    result = installed_recommended()
    assert result is None or isinstance(result, RecommendedFont)


# --- emoji support ----------------------------------------------------------------


def test_emoji_is_only_the_windows_consoles_problem() -> None:
    """Only the classic console of Windows has the emoji problem. Other terminals draw emoji.

    MeshTerm does not run the probe on other platforms. A Linux or macOS session has no
    cost from a limit that only the classic console has.
    """

    def never_called() -> bool | None:
        raise AssertionError("the console probe must not run off Windows")

    for system in ("linux", "darwin", "freebsd"):
        verdict = _emoji_support({}, console_probe=never_called, system=system)
        assert (verdict.supported, verdict.source) == (True, "platform")


def test_a_classic_console_turns_the_icons_compact() -> None:
    """The important verdict: conhost draws no emoji, so the icons become compact.

    This is the build that the user double-clicks on Windows. The whole check exists for
    this case.
    """
    verdict = _emoji_support({}, console_probe=lambda: True, system="win32")
    assert (verdict.supported, verdict.source) == (False, "console")


def test_every_other_windows_terminal_keeps_its_emoji() -> None:
    """Windows Terminal, the terminal of VS Code, and ssh all draw emoji, and nothing changes."""
    verdict = _emoji_support({}, console_probe=lambda: False, system="win32")
    assert (verdict.supported, verdict.source) == (True, "console")


def test_an_unanswerable_probe_never_degrades_the_ui() -> None:
    """If there is no console to identify (redirected output, a failed call), emoji stay on.

    An unknown result is not evidence of a limit. If MeshTerm degrades on an unknown
    result, it removes the icons from each piped or redirected run. These runs are most
    of the test suite and all of CI.
    """
    verdict = _emoji_support({}, console_probe=lambda: None, system="win32")
    assert (verdict.supported, verdict.source) == (True, "no-console")


def test_the_override_beats_the_probe_both_ways() -> None:
    """``MESHTERM_EMOJI`` wins in both directions, as the override of the powerline gate does.

    A user on a console that MeshTerm cannot recognize knows their own display better than
    a probe does. The negative direction is how a user asks for the compact icons on
    purpose.
    """
    forced_on = _emoji_support({"MESHTERM_EMOJI": "1"}, console_probe=lambda: True, system="win32")
    assert (forced_on.supported, forced_on.source) == (True, "env")

    def never_called() -> bool | None:
        raise AssertionError("the override decides before the probe runs")

    forced_off = _emoji_support({"MESHTERM_EMOJI": "0"}, console_probe=never_called, system="linux")
    assert (forced_off.supported, forced_off.source) == (False, "env")


def test_an_inherited_wt_session_cannot_speak_for_the_host() -> None:
    """MeshTerm does not use the environment. This test shows the reason.

    Child processes inherit ``WT_SESSION``. A program that starts from Windows Terminal
    into a console of its own still has it. We measured this, and did not suppose it. If
    MeshTerm reads the variable, the answer is for the terminal that started the program.
    """
    env = {"WT_SESSION": "670f6626-8bdf-4c09-9164-b1eef91abe4f"}
    verdict = _emoji_support(env, console_probe=lambda: True, system="win32")
    assert verdict.supported is False


def test_identifying_the_host_never_raises() -> None:
    """On any machine, the question is safe and the answer is one of three values.

    The function runs on the startup path. Thus the property "it degrades and does not
    raise" prevents an unusual host from stopping the app.
    """
    assert _is_classic_console() in (True, False, None)
