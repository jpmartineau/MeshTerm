# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and helpers: plain-text screen reading, and pinned path rendering.

:func:`plain` is THE way a test reads a rendered screen — every screen test needs the
same move (strip the ANSI colour escapes, join the rendered lines) before it can assert
on content, and each file re-deriving it is how the copies drift.

The powerline verdict is environmental — the suite may run inside VS Code or Windows
Terminal (both chip-capable) or a bare CI shell (not) — and screen assertions must
not change with the developer's glass. ``MESHTERM_POWERLINE=0`` is the supported pin;
the cached verdict is cleared around each test so no ordering leaks it. Powerline-
specific tests pass explicit modes or monkeypatch the widget's own switch.

``NO_COLOR`` is environmental the same way: Rich honours it by stripping colour while
keeping attributes, which silently voids every colour assertion in the suite (a shell
run by an agent harness sets it). The autouse fixture below deletes it, so the tests
always see the colours the app really emits.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Iterator

import pytest

from meshterm.platforms import REGULAR, set_platform
from meshterm.ui.termfont import powerline_support

#: ANSI SGR escapes — the colour runs a rendered line carries between its characters.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(rendered: str | Iterable[str]) -> str:
    """The plain-text form of rendered screen output, for content assertions.

    Takes what a render handed back — a list of ANSI lines (joined with newlines) or a
    single already-joined string — and strips the colour escapes, so assertions read
    the screen the way a viewer does.
    """
    if not isinstance(rendered, str):
        rendered = "\n".join(rendered)
    return _ANSI.sub("", rendered)


@pytest.fixture(autouse=True)
def _plain_path_rendering(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MESHTERM_POWERLINE", "0")
    # An inherited NO_COLOR makes Rich strip colour (attributes kept) and every colour
    # assertion silently reads bare text — see the module docstring.
    monkeypatch.delenv("NO_COLOR", raising=False)
    powerline_support.cache_clear()
    yield
    powerline_support.cache_clear()


@pytest.fixture
def powerline(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Pin the powerline verdict for a test that is *about* chips.

    The suite runs with ``MESHTERM_POWERLINE=0`` so screen assertions don't change with
    the developer's glass (see the module docstring); a test of the chip language itself
    needs the other answer, and asks for it through the supported override rather than by
    reaching into the widget. ``monkeypatch`` restores the env and the autouse fixture
    above clears the memoized verdict either side, so nothing leaks.
    """

    def _set(on: bool) -> None:
        monkeypatch.setenv("MESHTERM_POWERLINE", "1" if on else "0")
        powerline_support.cache_clear()

    return _set


@pytest.fixture(autouse=True)
def _reset_transmit_gate() -> Iterator[None]:
    """Every test starts having transmitted nothing.

    The transmit clock is process-wide (see :mod:`meshterm.core.transmit_gate`), which is
    right for a session and wrong for a suite: a test that sends anything would otherwise
    leave a cooldown standing for whatever ran next, and the next test's flow would stop
    on a countdown nobody scripted.
    """
    from meshterm.core.transmit_gate import current as transmit_clock

    transmit_clock().reset()
    yield
    transmit_clock().reset()


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """No test may reach the developer's own ``~/.meshterm``.

    ``$MESHTERM_HOME`` moves the whole directory — the history, the contact and channel
    caches, the outbox, the stored admin passwords, the device profiles, the preferences
    and the log. ``--db`` moves only the first of those, which is why isolating a CLI test
    with ``--db`` alone was not isolation at all: a synthetic contact written by a probe
    sat in the real cache and made ``test_cli_contract`` fail on one machine and pass on
    every other, and every run appended to the developer's real ``meshterm.log`` and read
    their real ``log_level``.

    Autouse and unconditional, because the exposure is not the CLI's: anything that
    touches ``Settings.load()``, a preference, a store or the logger has it. A test that
    wants a *populated* directory sets the variable itself; ``monkeypatch.setenv`` inside
    a test still wins over this.
    """
    home = tmp_path_factory.mktemp("meshterm-home")
    previous = os.environ.get("MESHTERM_HOME")
    os.environ["MESHTERM_HOME"] = str(home)
    yield
    if previous is None:
        os.environ.pop("MESHTERM_HOME", None)
    else:
        os.environ["MESHTERM_HOME"] = previous


@pytest.fixture(autouse=True)
def _reset_platform() -> Iterator[None]:
    """Every test starts and ends on :data:`~meshterm.platforms.REGULAR`.

    A test that calls ``set_platform(PICOCALC_LYRA)`` (directly, or via the gallery harness)
    can't leak that choice into whatever runs next — regular is the suite's baseline, the
    same way it is the default for a process that never passes ``--platform``/
    ``MESHTERM_PLATFORM`` (see :mod:`meshterm.platforms`).
    """
    set_platform(REGULAR)
    yield
    set_platform(REGULAR)
