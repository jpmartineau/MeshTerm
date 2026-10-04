# SPDX-License-Identifier: Apache-2.0
"""Session width tests: reclaiming the terminal's final column.

Some terminals (and prompt_toolkit's size probe on them) report the window one column
narrower than it really is, so the frame's right border lands one short and the true last
column sits unused. The session can report one extra column to close that gap. The wrapping
output is pure and the resolver is a small branch, so both are assertable without a terminal.

The default comes from the active :class:`~meshterm.platforms.Platform` (on, desktop; off,
PicoCalc — reclaiming a phantom column on its exact-width console would tear the frame), and
``MESHTERM_FULL_WIDTH`` remains an explicit override on top of that default either way.
"""

from __future__ import annotations

from types import SimpleNamespace

from prompt_toolkit.data_structures import Size

from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui.tui.session import TuiSession, _WidthExtendedOutput


def test_width_extended_output_reports_one_more_column() -> None:
    """get_size() gains a column; every other attribute forwards to the wrapped output."""
    inner = SimpleNamespace(
        get_size=lambda: Size(rows=24, columns=80),
        write=lambda s: f"wrote:{s}",
        encoding="utf-8",
    )
    out = _WidthExtendedOutput(inner)
    assert out.get_size() == Size(rows=24, columns=81)  # the reclaimed column
    assert out.write("x") == "wrote:x"  # forwarded method
    assert out.encoding == "utf-8"  # forwarded attribute


def test_session_leaves_a_supplied_output_untouched() -> None:
    """A test-supplied output is never wrapped — headless sizes stay exactly as set."""
    dummy = SimpleNamespace(get_size=lambda: Size(rows=10, columns=40))
    session = TuiSession(output=dummy)
    assert session._resolve_output() is dummy


def test_session_wraps_the_real_terminal_on_regular(monkeypatch) -> None:
    """No supplied output + REGULAR (today's default) → the real terminal is width-extended."""
    fake = SimpleNamespace(get_size=lambda: Size(rows=30, columns=100))
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: fake)
    monkeypatch.delenv("MESHTERM_FULL_WIDTH", raising=False)
    set_platform(REGULAR)
    out = TuiSession()._resolve_output()
    assert isinstance(out, _WidthExtendedOutput)
    assert out.get_size() == Size(rows=30, columns=101)


def test_session_pins_the_real_terminal_inside_the_width_extension(monkeypatch) -> None:
    """Both wraps on REGULAR, reclaim outermost: its size is what everything lays out against."""
    from meshterm.ui.tui.colsnap import PinnedOutput

    fake = SimpleNamespace(get_size=lambda: Size(rows=30, columns=100))
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: fake)
    monkeypatch.delenv("MESHTERM_FULL_WIDTH", raising=False)
    monkeypatch.delenv("MESHTERM_COLUMN_SNAP", raising=False)
    out = TuiSession()._resolve_output()
    assert isinstance(out, _WidthExtendedOutput)
    assert isinstance(out._inner, PinnedOutput) and out._inner._inner is fake

    # Pinning alone, where the column is not reclaimed.
    monkeypatch.setenv("MESHTERM_FULL_WIDTH", "0")
    pinned = TuiSession()._resolve_output()
    assert isinstance(pinned, PinnedOutput) and pinned._inner is fake


def test_session_leaves_the_bare_terminal_on_picocalc(monkeypatch) -> None:
    """PICOCALC_LYRA's exact-width console → no reclaim: a phantom column would tear the frame."""
    monkeypatch.delenv("MESHTERM_FULL_WIDTH", raising=False)
    set_platform(PICOCALC_LYRA)
    assert TuiSession()._resolve_output() is None


def test_env_override_forces_reclaim_on_despite_picocalc(monkeypatch) -> None:
    """``MESHTERM_FULL_WIDTH=1`` wins over PICOCALC_LYRA's off-by-default."""
    fake = SimpleNamespace(get_size=lambda: Size(rows=30, columns=100))
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: fake)
    monkeypatch.setenv("MESHTERM_FULL_WIDTH", "1")
    set_platform(PICOCALC_LYRA)
    assert isinstance(TuiSession()._resolve_output(), _WidthExtendedOutput)


def test_env_override_forces_reclaim_off_despite_regular(monkeypatch) -> None:
    """``MESHTERM_FULL_WIDTH=0`` wins over REGULAR's on-by-default.

    The terminal is still wrapped for pinning, which has a gate of its own; with that turned
    off too there is nothing left to wrap, and prompt_toolkit builds its own output.
    """
    fake = SimpleNamespace(get_size=lambda: Size(rows=30, columns=100))
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: fake)
    monkeypatch.setenv("MESHTERM_FULL_WIDTH", "0")
    monkeypatch.delenv("MESHTERM_COLUMN_SNAP", raising=False)
    set_platform(REGULAR)
    out = TuiSession()._resolve_output()
    assert not isinstance(out, _WidthExtendedOutput)
    assert out.get_size() == Size(rows=30, columns=100)  # no column reclaimed

    monkeypatch.setenv("MESHTERM_COLUMN_SNAP", "0")
    assert TuiSession()._resolve_output() is None
