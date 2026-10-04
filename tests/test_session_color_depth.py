# SPDX-License-Identifier: Apache-2.0
"""Session colour tests: how many colours the terminal is actually sent.

prompt_toolkit decides this per *output class*, and its two classes disagree —
``Windows10_Output`` returns true colour outright, ``Vt100_Output`` returns 256 for every
``TERM`` but ``linux`` — so the same theme was drawn at 24 bits on Windows and quantized to
the 216-colour cube on macOS and Linux, silently. The resolver closes that gap, and these
tests pin the one property that makes it safe to run everywhere: it only ever *raises* the
verdict. A terminal that cannot be shown to do better returns ``None`` and keeps exactly the
depth prompt_toolkit gave it, because guessing 24-bit at a terminal without it costs not a
duller palette but the colour entirely.
"""

from __future__ import annotations

from prompt_toolkit.output.color_depth import ColorDepth

from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui.tui.session import _color_depth


def _auto(monkeypatch) -> None:
    """Clear both overrides so the environment reading is what is under test."""
    monkeypatch.delenv("MESHTERM_COLOR_DEPTH", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("TERM", raising=False)


def test_an_unremarkable_terminal_keeps_prompt_toolkits_own_verdict(monkeypatch) -> None:
    """Nothing claimed → ``None``, so a host we have never heard of is never made worse."""
    _auto(monkeypatch)
    set_platform(REGULAR)
    assert _color_depth() is None


def test_colorterm_truecolor_raises_the_depth(monkeypatch) -> None:
    """The convention every truecolor terminal follows — and the one prompt_toolkit ignores."""
    _auto(monkeypatch)
    monkeypatch.setenv("COLORTERM", "truecolor")
    set_platform(REGULAR)
    assert _color_depth() is ColorDepth.DEPTH_24_BIT


def test_colorterm_24bit_is_the_same_claim(monkeypatch) -> None:
    """``24bit`` is the other spelling in circulation; case is not part of the claim."""
    _auto(monkeypatch)
    monkeypatch.setenv("COLORTERM", "24BIT")
    set_platform(REGULAR)
    assert _color_depth() is ColorDepth.DEPTH_24_BIT


def test_a_direct_colour_terminfo_entry_raises_the_depth(monkeypatch) -> None:
    """``*-direct`` is terminfo's own spelling for a direct-colour terminal."""
    _auto(monkeypatch)
    monkeypatch.setenv("TERM", "xterm-direct")
    set_platform(REGULAR)
    assert _color_depth() is ColorDepth.DEPTH_24_BIT


def test_picocalc_is_never_promoted(monkeypatch) -> None:
    """A 16-slot console addresses its palette by index; RGB has nowhere to land there."""
    _auto(monkeypatch)
    monkeypatch.setenv("COLORTERM", "truecolor")
    set_platform(PICOCALC_LYRA)
    assert _color_depth() is None


def test_env_override_names_a_depth_outright(monkeypatch) -> None:
    """``MESHTERM_COLOR_DEPTH`` wins over the reading, in either direction."""
    _auto(monkeypatch)
    monkeypatch.setenv("COLORTERM", "truecolor")
    set_platform(REGULAR)
    monkeypatch.setenv("MESHTERM_COLOR_DEPTH", "256")
    assert _color_depth() is ColorDepth.DEPTH_8_BIT
    monkeypatch.setenv("MESHTERM_COLOR_DEPTH", "16")
    assert _color_depth() is ColorDepth.DEPTH_4_BIT


def test_the_preference_is_consulted_below_the_env_override(monkeypatch) -> None:
    """A stored preference names a depth the same way; ``auto`` falls through to the reading."""
    from types import SimpleNamespace

    _auto(monkeypatch)
    set_platform(REGULAR)
    monkeypatch.setattr(
        "meshterm.core.preferences.current",
        lambda: SimpleNamespace(color_depth="truecolor"),
    )
    assert _color_depth() is ColorDepth.DEPTH_24_BIT
    monkeypatch.setattr(
        "meshterm.core.preferences.current",
        lambda: SimpleNamespace(color_depth="auto"),
    )
    assert _color_depth() is None


def test_an_unrecognised_preference_value_changes_nothing(monkeypatch) -> None:
    """A hand-edited preference value we cannot read is not grounds for guessing."""
    from types import SimpleNamespace

    _auto(monkeypatch)
    monkeypatch.setenv("COLORTERM", "truecolor")
    set_platform(REGULAR)
    monkeypatch.setattr(
        "meshterm.core.preferences.current",
        lambda: SimpleNamespace(color_depth="lots"),
    )
    assert _color_depth() is None
