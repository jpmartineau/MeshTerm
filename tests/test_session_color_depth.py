# SPDX-License-Identifier: Apache-2.0
"""Session colour tests: the number of colours that MeshTerm sends to the terminal.

prompt_toolkit decides this for each output class, and its two classes do not agree.
``Windows10_Output`` returns true colour. ``Vt100_Output`` returns 256 for each ``TERM``
except ``linux``. Thus the same theme was drawn at 24 bits on Windows, but it was quantized
to the 216-colour cube on macOS and Linux, with no message. The resolver closes this gap.
These tests prove the one property that makes the resolver safe on each platform: it only
raises the verdict. If a terminal cannot be shown to do better, the resolver returns
``None``, and the terminal keeps the depth that prompt_toolkit gave it. A wrong guess of
24 bits for a terminal that does not have it does not give a duller palette. The terminal
loses the colour completely.
"""

from __future__ import annotations

from prompt_toolkit.output.color_depth import ColorDepth

from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui.tui.session import _color_depth


def _auto(monkeypatch) -> None:
    """Clear both overrides, so that the test examines the reading of the environment."""
    monkeypatch.delenv("MESHTERM_COLOR_DEPTH", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("TERM", raising=False)


def test_an_unremarkable_terminal_keeps_prompt_toolkits_own_verdict(monkeypatch) -> None:
    """If the terminal claims nothing, the result is ``None``.

    Thus an unknown host is never made worse.
    """
    _auto(monkeypatch)
    set_platform(REGULAR)
    assert _color_depth() is None


def test_colorterm_truecolor_raises_the_depth(monkeypatch) -> None:
    """Each truecolor terminal follows this convention, but prompt_toolkit ignores it."""
    _auto(monkeypatch)
    monkeypatch.setenv("COLORTERM", "truecolor")
    set_platform(REGULAR)
    assert _color_depth() is ColorDepth.DEPTH_24_BIT


def test_colorterm_24bit_is_the_same_claim(monkeypatch) -> None:
    """``24bit`` is the other spelling in use. The case of the letters does not change the claim."""
    _auto(monkeypatch)
    monkeypatch.setenv("COLORTERM", "24BIT")
    set_platform(REGULAR)
    assert _color_depth() is ColorDepth.DEPTH_24_BIT


def test_a_direct_colour_terminfo_entry_raises_the_depth(monkeypatch) -> None:
    """``*-direct`` is the spelling of terminfo for a direct-colour terminal."""
    _auto(monkeypatch)
    monkeypatch.setenv("TERM", "xterm-direct")
    set_platform(REGULAR)
    assert _color_depth() is ColorDepth.DEPTH_24_BIT


def test_picocalc_is_never_promoted(monkeypatch) -> None:
    """A console with 16 slots uses an index for its palette. RGB values have no place there."""
    _auto(monkeypatch)
    monkeypatch.setenv("COLORTERM", "truecolor")
    set_platform(PICOCALC_LYRA)
    assert _color_depth() is None


def test_env_override_names_a_depth_outright(monkeypatch) -> None:
    """``MESHTERM_COLOR_DEPTH`` overrides the reading, in each direction."""
    _auto(monkeypatch)
    monkeypatch.setenv("COLORTERM", "truecolor")
    set_platform(REGULAR)
    monkeypatch.setenv("MESHTERM_COLOR_DEPTH", "256")
    assert _color_depth() is ColorDepth.DEPTH_8_BIT
    monkeypatch.setenv("MESHTERM_COLOR_DEPTH", "16")
    assert _color_depth() is ColorDepth.DEPTH_4_BIT


def test_the_preference_is_consulted_below_the_env_override(monkeypatch) -> None:
    """A stored preference names a depth in the same way. ``auto`` goes on to the reading."""
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
    """If MeshTerm cannot read a preference value that the user edited, it does not guess."""
    from types import SimpleNamespace

    _auto(monkeypatch)
    monkeypatch.setenv("COLORTERM", "truecolor")
    set_platform(REGULAR)
    monkeypatch.setattr(
        "meshterm.core.preferences.current",
        lambda: SimpleNamespace(color_depth="lots"),
    )
    assert _color_depth() is None
