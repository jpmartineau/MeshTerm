# SPDX-License-Identifier: Apache-2.0
"""Session width tests: reclaiming the terminal's final column.

prompt_toolkit's Windows console output reports the window one column narrower than it
really is, so the frame's right border lands one short and the true last column sits unused.
The session can report one extra column to close that gap. The wrapping output is pure and
the resolver is a small branch, so both are assertable without a terminal.

The reclaim is right only where the probe hid the column. On an exact-width terminal (every
POSIX one) the phantom column overprints the real last cell, which on Linux turned a full
battery's ``100`` into ``10``. So by default the terminal's own output decides; the platform
can only rule it out (PicoCalc's exact-width console), and ``MESHTERM_FULL_WIDTH`` remains an
explicit override on top of either.
"""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output.vt100 import Vt100_Output

from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui.tui.session import TuiSession, _probe_hides_last_column, _WidthExtendedOutput


def _vt100(columns: int = 100) -> Vt100_Output:
    """A POSIX terminal's output, sized exactly as ``TIOCGWINSZ`` would size it."""
    size = Size(rows=30, columns=columns)
    return Vt100_Output(io.StringIO(), lambda: size, term="xterm-256color", enable_cpr=False)


@pytest.fixture
def windows_console(monkeypatch):
    """Make whatever the session builds read as prompt_toolkit's Windows console output."""
    monkeypatch.setattr("meshterm.ui.tui.session._probe_hides_last_column", lambda _out: True)


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


def test_session_widens_a_windows_console_on_regular(monkeypatch, windows_console) -> None:
    """No supplied output + REGULAR + a probe that hid a column → the column is reclaimed."""
    fake = SimpleNamespace(get_size=lambda: Size(rows=30, columns=100))
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: fake)
    monkeypatch.delenv("MESHTERM_FULL_WIDTH", raising=False)
    set_platform(REGULAR)
    out = TuiSession()._resolve_output()
    assert isinstance(out, _WidthExtendedOutput)
    assert out.get_size() == Size(rows=30, columns=101)


def test_session_leaves_an_exact_terminal_its_own_width_on_regular(monkeypatch) -> None:
    """A POSIX terminal is sized exactly, so REGULAR claims no phantom column on it.

    The phantom column used to be claimed here too. With autowrap off, its cell overprinted the
    real last one, so the flush-right battery gauge lost its final character on Linux: a full
    pack read ``10``, and 99% read ``9%``.
    """
    terminal = _vt100(columns=100)
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: terminal)
    monkeypatch.delenv("MESHTERM_FULL_WIDTH", raising=False)
    monkeypatch.delenv("MESHTERM_COLUMN_SNAP", raising=False)
    set_platform(REGULAR)
    out = TuiSession()._resolve_output()
    assert not isinstance(out, _WidthExtendedOutput)
    assert out.get_size() == Size(rows=30, columns=100)

    # And with pinning off too, nothing is left to wrap: prompt_toolkit builds its own.
    monkeypatch.setenv("MESHTERM_COLUMN_SNAP", "0")
    assert TuiSession()._resolve_output() is None


def test_only_the_windows_console_probe_hides_a_column() -> None:
    """The probe question is answered by the output's kind: exact everywhere but Win32."""
    assert _probe_hides_last_column(_vt100()) is False
    assert _probe_hides_last_column(SimpleNamespace()) is False
    if sys.platform != "win32":
        return
    from prompt_toolkit.output.win32 import Win32Output

    win32 = object.__new__(Win32Output)  # no console needed to ask what kind it is
    assert _probe_hides_last_column(win32) is True
    # Windows10_Output and ConEmuOutput hold one and hand their size to it.
    assert _probe_hides_last_column(SimpleNamespace(win32_output=win32)) is True


def test_session_pins_the_real_terminal_inside_the_width_extension(
    monkeypatch, windows_console
) -> None:
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
