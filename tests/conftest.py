# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and helpers: plain-text screen reading, and fixed path rendering.

:func:`plain` is the only way that a test reads a rendered screen. Each screen test must
first remove the ANSI colour escapes and join the rendered lines. Only then can it make
assertions about the content. If each file did this again, the copies would differ over
time.

The powerline verdict depends on the environment. The suite can run inside VS Code or
Windows Terminal (both can draw chips) or in a bare CI shell (which cannot). The screen
assertions must not change with the terminal of the developer. ``MESHTERM_POWERLINE=0``
is the supported way to fix the verdict. The suite clears the cached verdict around each
test, so that the order of the tests has no effect. The tests that are specific to
powerline pass explicit modes, or they patch the widget's own switch with
``monkeypatch``.

``NO_COLOR`` depends on the environment in the same way. Rich obeys it: it removes colour
and keeps attributes. This silently makes each colour assertion in the suite useless (a
shell that an agent harness runs sets it). The autouse fixture below deletes it. Thus the
tests always see the colours that the app really sends.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Iterator

import pytest

from meshterm.platforms import REGULAR, set_platform
from meshterm.ui.termfont import powerline_support

#: ANSI SGR escapes: the colour runs that a rendered line has between its characters.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(rendered: str | Iterable[str]) -> str:
    """The plain-text form of rendered screen output, for content assertions.

    The function takes the result of a render: a list of ANSI lines (which it joins with
    newlines), or one string that is already joined. It removes the colour escapes. Thus
    the assertions read the screen in the same way as a viewer.
    """
    if not isinstance(rendered, str):
        rendered = "\n".join(rendered)
    return _ANSI.sub("", rendered)


@pytest.fixture(autouse=True)
def _plain_path_rendering(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MESHTERM_POWERLINE", "0")
    # An inherited NO_COLOR makes Rich remove colour (and keep attributes). Then each
    # colour assertion silently reads bare text. Refer to the module docstring.
    monkeypatch.delenv("NO_COLOR", raising=False)
    powerline_support.cache_clear()
    yield
    powerline_support.cache_clear()


@pytest.fixture
def powerline(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Fix the powerline verdict for a test that is about chips.

    The suite runs with ``MESHTERM_POWERLINE=0``. Thus the screen assertions do not change
    with the terminal of the developer (refer to the module docstring). A test of the chip
    language needs the other answer. The test asks for it through the supported override,
    and does not go into the widget. ``monkeypatch`` restores the environment, and the
    autouse fixture above clears the cached verdict before and after. Thus no state stays.
    """

    def _set(on: bool) -> None:
        monkeypatch.setenv("MESHTERM_POWERLINE", "1" if on else "0")
        powerline_support.cache_clear()

    return _set


@pytest.fixture(autouse=True)
def _reset_transmit_gate() -> Iterator[None]:
    """Each test starts with nothing transmitted.

    The transmit clock is for the whole process (refer to :mod:`meshterm.core.transmit_gate`).
    This is correct for a session and wrong for a suite. If a test sent anything, it would
    leave a cooldown that is still active for the next test. Then the flow of the next test
    would stop on a countdown that nobody wrote in the script.
    """
    from meshterm.core.transmit_gate import current as transmit_clock

    transmit_clock().reset()
    yield
    transmit_clock().reset()


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """No test can reach the developer's own ``~/.meshterm``.

    ``$MESHTERM_HOME`` moves the whole directory: the history, the caches of contacts and
    channels, the outbox, the stored admin passwords, the device profiles, the preferences,
    and the log. ``--db`` moves only the first of these. Thus a CLI test that used only
    ``--db`` was not isolated. A synthetic contact that a probe wrote stayed in the real
    cache. This made ``test_cli_contract`` fail on one machine and pass on each other
    machine. Also, each run added lines to the real ``meshterm.log`` of the developer, and
    read the real ``log_level``.

    The fixture is autouse and unconditional, because the risk is not only in the CLI.
    Any code that uses ``Settings.load()``, a preference, a store, or the logger has it.
    A test that needs a directory with data sets the variable itself.
    ``monkeypatch.setenv`` in a test still wins over this fixture.
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
    """Each test starts and ends on :data:`~meshterm.platforms.REGULAR`.

    A test can call ``set_platform(PICOCALC_LYRA)`` (directly, or through the gallery
    harness). That choice cannot stay for the next test. Regular is the baseline of the
    suite. It is also the default for a process that does not get ``--platform`` or
    ``MESHTERM_PLATFORM`` (refer to :mod:`meshterm.platforms`).
    """
    set_platform(REGULAR)
    yield
    set_platform(REGULAR)
