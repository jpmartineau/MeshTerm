# SPDX-License-Identifier: Apache-2.0
"""Session width tests: the reclaim of the last column of the terminal.

The Windows console output of prompt_toolkit reports a window that is one column narrower
than the real window. Thus the right border of the frame is one column short, and the real
last column is not used. The session can report one more column to close this gap. The
wrapping output is pure and the resolver is a small branch, so the tests can check both
without a terminal.

The reclaim is correct only where the probe hid the column. On a terminal with an exact
width (each POSIX terminal), the phantom column overprints the real last cell. On Linux,
this changed the ``100`` of a full battery to ``10``. Thus, by default, the output of the
terminal decides. The platform can only rule the reclaim out (the exact-width console of the
PicoCalc). ``MESHTERM_FULL_WIDTH`` stays as an explicit override on top of both.
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
    """The output of a POSIX terminal, with the size that ``TIOCGWINSZ`` gives."""
    size = Size(rows=30, columns=columns)
    return Vt100_Output(io.StringIO(), lambda: size, term="xterm-256color", enable_cpr=False)


@pytest.fixture
def windows_console(monkeypatch):
    """Make the session treat its output as the Windows console output of prompt_toolkit."""
    monkeypatch.setattr("meshterm.ui.tui.session._probe_hides_last_column", lambda _out: True)


def test_width_extended_output_reports_one_more_column() -> None:
    """get_size() gets one more column. Each other attribute goes to the wrapped output."""
    inner = SimpleNamespace(
        get_size=lambda: Size(rows=24, columns=80),
        write=lambda s: f"wrote:{s}",
        encoding="utf-8",
    )
    out = _WidthExtendedOutput(inner)
    assert out.get_size() == Size(rows=24, columns=81)  # the reclaimed column
    assert out.write("x") == "wrote:x"  # a method that goes to the wrapped output
    assert out.encoding == "utf-8"  # an attribute that goes to the wrapped output


def test_session_leaves_a_supplied_output_untouched() -> None:
    """A session does not wrap an output that a test supplies. Headless sizes stay as set."""
    dummy = SimpleNamespace(get_size=lambda: Size(rows=10, columns=40))
    session = TuiSession(output=dummy)
    assert session._resolve_output() is dummy


def test_session_widens_a_windows_console_on_regular(monkeypatch, windows_console) -> None:
    """With no supplied output, REGULAR, and a probe that hid a column, the session reclaims it."""
    fake = SimpleNamespace(get_size=lambda: Size(rows=30, columns=100))
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: fake)
    monkeypatch.delenv("MESHTERM_FULL_WIDTH", raising=False)
    set_platform(REGULAR)
    out = TuiSession()._resolve_output()
    assert isinstance(out, _WidthExtendedOutput)
    assert out.get_size() == Size(rows=30, columns=101)


def test_session_leaves_an_exact_terminal_its_own_width_on_regular(monkeypatch) -> None:
    """A POSIX terminal has an exact size, so REGULAR does not claim a phantom column on it.

    REGULAR once claimed the phantom column here too. With autowrap off, the cell of that
    column overprinted the real last cell. Thus the battery gauge, which is flush right, lost
    its last character on Linux: a full battery showed ``10``, and 99% showed ``9%``.
    """
    terminal = _vt100(columns=100)
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: terminal)
    monkeypatch.delenv("MESHTERM_FULL_WIDTH", raising=False)
    monkeypatch.delenv("MESHTERM_COLUMN_SNAP", raising=False)
    set_platform(REGULAR)
    out = TuiSession()._resolve_output()
    assert not isinstance(out, _WidthExtendedOutput)
    assert out.get_size() == Size(rows=30, columns=100)

    # If pinning is also off, nothing is left to wrap, and prompt_toolkit builds its own.
    monkeypatch.setenv("MESHTERM_COLUMN_SNAP", "0")
    assert TuiSession()._resolve_output() is None


def test_only_the_windows_console_probe_hides_a_column() -> None:
    """The type of the output answers the probe question: exact everywhere but on Win32."""
    assert _probe_hides_last_column(_vt100()) is False
    assert _probe_hides_last_column(SimpleNamespace()) is False
    if sys.platform != "win32":
        return
    from prompt_toolkit.output.win32 import Win32Output

    win32 = object.__new__(Win32Output)  # no console is necessary to ask for its type
    assert _probe_hides_last_column(win32) is True
    # Windows10_Output and ConEmuOutput each hold one, and give their size to it.
    assert _probe_hides_last_column(SimpleNamespace(win32_output=win32)) is True


def test_session_pins_the_real_terminal_inside_the_width_extension(
    monkeypatch, windows_console
) -> None:
    """REGULAR has both wraps, and the reclaim is the outer one. Each layout uses its size."""
    from meshterm.ui.tui.colsnap import PinnedOutput

    fake = SimpleNamespace(get_size=lambda: Size(rows=30, columns=100))
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: fake)
    monkeypatch.delenv("MESHTERM_FULL_WIDTH", raising=False)
    monkeypatch.delenv("MESHTERM_COLUMN_SNAP", raising=False)
    out = TuiSession()._resolve_output()
    assert isinstance(out, _WidthExtendedOutput)
    assert isinstance(out._inner, PinnedOutput) and out._inner._inner is fake

    # Pinning alone, where the session does not reclaim the column.
    monkeypatch.setenv("MESHTERM_FULL_WIDTH", "0")
    pinned = TuiSession()._resolve_output()
    assert isinstance(pinned, PinnedOutput) and pinned._inner is fake


def test_session_leaves_the_bare_terminal_on_picocalc(monkeypatch) -> None:
    """PICOCALC_LYRA has an exact-width console, so there is no reclaim.

    A phantom column tears the frame.
    """
    monkeypatch.delenv("MESHTERM_FULL_WIDTH", raising=False)
    set_platform(PICOCALC_LYRA)
    assert TuiSession()._resolve_output() is None


def test_env_override_forces_reclaim_on_despite_picocalc(monkeypatch) -> None:
    """``MESHTERM_FULL_WIDTH=1`` overrides the default of PICOCALC_LYRA, which is off."""
    fake = SimpleNamespace(get_size=lambda: Size(rows=30, columns=100))
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: fake)
    monkeypatch.setenv("MESHTERM_FULL_WIDTH", "1")
    set_platform(PICOCALC_LYRA)
    assert isinstance(TuiSession()._resolve_output(), _WidthExtendedOutput)


def test_env_override_forces_reclaim_off_despite_regular(monkeypatch) -> None:
    """``MESHTERM_FULL_WIDTH=0`` overrides the default of REGULAR, which is on.

    The session still wraps the terminal for pinning, which has its own gate. If that gate is
    also off, nothing is left to wrap, and prompt_toolkit builds its own output.
    """
    fake = SimpleNamespace(get_size=lambda: Size(rows=30, columns=100))
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: fake)
    monkeypatch.setenv("MESHTERM_FULL_WIDTH", "0")
    monkeypatch.delenv("MESHTERM_COLUMN_SNAP", raising=False)
    set_platform(REGULAR)
    out = TuiSession()._resolve_output()
    assert not isinstance(out, _WidthExtendedOutput)
    assert out.get_size() == Size(rows=30, columns=100)  # the session reclaims no column

    monkeypatch.setenv("MESHTERM_COLUMN_SNAP", "0")
    assert TuiSession()._resolve_output() is None
