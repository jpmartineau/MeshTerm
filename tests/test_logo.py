# SPDX-License-Identifier: Apache-2.0
"""The splash art's colour rewriting: brightness said as a colour, never as bold.

The art is DOS-era ANSI, where `1m` lifts the foreground into the bright bank and every
span after it inherits that. Neither half of that survives the trip here: rows are handed
to Rich one at a time, so nothing inherits, and on macOS Terminal bold asks for a heavier
face and leaves the colour alone. So `_state_intensity` restates brightness as the
aixterm bright bank (`9N`), which can only ever mean a colour.

These pin the part that is easy to get subtly wrong — a sequence that changes the
intensity while naming no colour at all, which is exactly how the art announces a bright
run. Both splash files are full of bare `1m`, and dropping one silently draws a whole run
in the dim bank: two spans meant to read dark then light both come out dark.
"""

from __future__ import annotations

import re
from pathlib import Path

from meshterm.ui.logo import _state_intensity

_SPLASH = Path(__file__).resolve().parent.parent / "meshterm" / "assets" / "splash"

_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _params(text: str) -> list[list[str]]:
    """Every SGR sequence in `text`, split into its parameters.

    Matched as whole tokens rather than by substring: a pattern like ``[0-9;]*1`` also
    matches the trailing digit of ``31``, which would read a perfectly ordinary red as a
    request for bold.
    """
    return [found.group(1).split(";") for found in _SGR.finditer(text)]


def _brights(text: str) -> int:
    """How many sequences in `text` name a bright-bank foreground."""
    return sum(any(p.isdigit() and 90 <= int(p) <= 97 for p in parts) for parts in _params(text))


# -- a bare intensity change ----------------------------------------------------------


def test_bare_bright_lifts_the_colour_already_in_force() -> None:
    """`1m` on its own brightens the standing foreground, not just the next one named."""
    out = _state_intensity("\x1b[0;37mdim\x1b[1mbright")
    assert "\x1b[97m" in out, out.replace("\x1b", "ESC")


def test_bare_bright_carries_a_background_without_losing_the_foreground() -> None:
    """`1;41m` names a background and no foreground; the standing one still brightens."""
    out = _state_intensity("\x1b[0;32mgreen\x1b[1;41mbright on red")
    assert "\x1b[41;92m" in out, out.replace("\x1b", "ESC")


def test_returning_to_dim_restates_the_colour_in_the_dim_bank() -> None:
    """`22m` has the same problem in reverse: the run would stay in the bright bank."""
    out = _state_intensity("\x1b[0;31mred\x1b[1mbright\x1b[22mdim again")
    assert out.endswith("dim again")
    assert "\x1b[22;31m" in out, out.replace("\x1b", "ESC")


def test_bright_after_a_reset_is_white() -> None:
    """After `0m` the colour in force is the console's default grey, so `1m` means white.

    The splash's letters say white exactly this way (`0m` then `1m`); read as "no colour to
    restate", the run fell to the terminal's own default and a white row drew grey.
    """
    out = _state_intensity("\x1b[0;36mcyan\x1b[0m\x1b[1mwhite")
    tail = out.split("white")[0].split("\x1b[0m")[-1]
    assert "\x1b[97m" in tail, out.replace("\x1b", "ESC")


def test_a_named_foreground_is_still_rewritten_in_place() -> None:
    """The original behaviour: `1;33m` becomes `93m`, with the bold dropped."""
    out = _state_intensity("\x1b[0m\x1b[1;33myellow")
    assert "\x1b[93m" in out, out.replace("\x1b", "ESC")


# -- the art itself -------------------------------------------------------------------


def test_no_splash_row_asks_a_terminal_for_bold() -> None:
    """Bold is a font weight on macOS Terminal, so none may reach the screen."""
    for art in sorted(_SPLASH.glob("*.ans")):
        raw = art.read_bytes().decode("cp437", errors="replace")
        for number, line in enumerate(raw.split("\n"), start=1):
            for parts in _params(_state_intensity(line)):
                assert "1" not in parts, f"{art.name}:{number} still asks for bold"


def test_no_bright_run_in_the_art_is_silently_dropped() -> None:
    """Every `1m` must leave a bright-bank foreground behind.

    A colour is always standing: the one last named, or after a reset the console's default
    grey — which a bare `1m` lifts to white, the case the splash's lettering is drawn in.

    Counting brights across a file cannot catch this -- one `1m` brightens every later
    sequence, so the totals stay comfortably high while individual runs go missing. The
    invariant is per sequence: every sequence in the source that turns brightness on must
    produce a bright-bank emission in the rewritten row.
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
