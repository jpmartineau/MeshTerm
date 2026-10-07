# SPDX-License-Identifier: Apache-2.0
"""The colour rewriting of the splash art: brightness is a colour, and never bold.

The art is ANSI from the DOS era. In it, `1m` lifts the foreground into the bright bank,
and each span after it inherits that. Neither part of this works here. The code gives rows
to Rich one at a time, so nothing inherits. Also, on macOS Terminal, bold asks for a
heavier face and does not change the colour. Thus `_state_intensity` states brightness
again as the aixterm bright bank (`9N`), which can only mean a colour.

These tests check the part that is easy to get wrong in a way that is hard to see. It is a
sequence that changes the intensity and names no colour. This is exactly how the art
starts a bright run. Both splash files have many bare `1m` sequences. If the code removes
one with no message, it draws a whole run in the dim bank. Then two spans that must read
dark and then light both come out dark.
"""

from __future__ import annotations

import re
from pathlib import Path

from meshterm.ui.logo import _state_intensity

_SPLASH = Path(__file__).resolve().parent.parent / "meshterm" / "assets" / "splash"

_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _params(text: str) -> list[list[str]]:
    """Each SGR sequence in `text`, split into its parameters.

    The function matches whole tokens and not substrings. A pattern such as ``[0-9;]*1``
    also matches the last digit of ``31``. Then an ordinary red would look like a request
    for bold.
    """
    return [found.group(1).split(";") for found in _SGR.finditer(text)]


def _brights(text: str) -> int:
    """The number of sequences in `text` that name a bright-bank foreground."""
    return sum(any(p.isdigit() and 90 <= int(p) <= 97 for p in parts) for parts in _params(text))


# -- a bare intensity change ----------------------------------------------------------


def test_bare_bright_lifts_the_colour_already_in_force() -> None:
    """A bare bright sequence lifts the colour that is already in force.

    `1m` alone makes the current foreground brighter. It does not only affect the next
    colour that the art names.
    """
    out = _state_intensity("\x1b[0;37mdim\x1b[1mbright")
    assert "\x1b[97m" in out, out.replace("\x1b", "ESC")


def test_bare_bright_carries_a_background_without_losing_the_foreground() -> None:
    """A bare bright sequence keeps a background and does not lose the foreground.

    `1;41m` names a background and no foreground. The current foreground still becomes
    brighter.
    """
    out = _state_intensity("\x1b[0;32mgreen\x1b[1;41mbright on red")
    assert "\x1b[41;92m" in out, out.replace("\x1b", "ESC")


def test_returning_to_dim_restates_the_colour_in_the_dim_bank() -> None:
    """A return to dim states the colour again in the dim bank.

    `22m` has the same problem in the other direction. If the code did not state the
    colour again, the run would stay in the bright bank.
    """
    out = _state_intensity("\x1b[0;31mred\x1b[1mbright\x1b[22mdim again")
    assert out.endswith("dim again")
    assert "\x1b[22;31m" in out, out.replace("\x1b", "ESC")


def test_bright_after_a_reset_is_white() -> None:
    """A bright sequence after a reset is white.

    After `0m`, the colour in force is the default grey of the console, so `1m` means
    white. The letters of the splash say white in exactly this way (`0m` then `1m`). When
    the code read this as "no colour to state again", the run used the own default of the
    terminal, and a white row was drawn grey.
    """
    out = _state_intensity("\x1b[0;36mcyan\x1b[0m\x1b[1mwhite")
    tail = out.split("white")[0].split("\x1b[0m")[-1]
    assert "\x1b[97m" in tail, out.replace("\x1b", "ESC")


def test_a_named_foreground_is_still_rewritten_in_place() -> None:
    """A named foreground is still rewritten in its place.

    This is the original behaviour: `1;33m` becomes `93m`, and the bold is removed.
    """
    out = _state_intensity("\x1b[0m\x1b[1;33myellow")
    assert "\x1b[93m" in out, out.replace("\x1b", "ESC")


# -- the art itself -------------------------------------------------------------------


def test_no_splash_row_asks_a_terminal_for_bold() -> None:
    """No row of the splash asks a terminal for bold.

    Bold is a font weight on macOS Terminal, so no bold sequence can reach the screen.
    """
    for art in sorted(_SPLASH.glob("*.ans")):
        raw = art.read_bytes().decode("cp437", errors="replace")
        for number, line in enumerate(raw.split("\n"), start=1):
            for parts in _params(_state_intensity(line)):
                assert "1" not in parts, f"{art.name}:{number} still asks for bold"


def test_no_bright_run_in_the_art_is_silently_dropped() -> None:
    """No bright run in the art is removed with no message.

    Each `1m` must leave a bright-bank foreground. A colour is always in force. It is the
    colour that the art named last. After a reset, it is the default grey of the console.
    A bare `1m` lifts this grey to white, which is the case for the lettering of the
    splash.

    A count of the bright sequences in a file cannot find this problem. One `1m` makes each
    later sequence brighter, so the totals stay high while single runs are lost. The
    invariant is for each sequence. Each sequence in the source that turns brightness on
    must give a bright-bank sequence in the rewritten row.
    """
    for art in sorted(_SPLASH.glob("*.ans")):
        raw = art.read_bytes().decode("cp437", errors="replace")
        for number, line in enumerate(raw.split("\n"), start=1):
            owed = 0
            for parts in _params(line):
                if "1" in parts:
                    owed += 1
            if owed:
                assert _brights(_state_intensity(line)) >= owed, (
                    f"{art.name}:{number} turns brightness on {owed} time(s) with a "
                    f"colour standing, but the rewrite emits "
                    f"{_brights(_state_intensity(line))} bright foreground(s)"
                )
