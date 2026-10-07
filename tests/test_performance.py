# SPDX-License-Identifier: Apache-2.0
"""The P2 performance contracts: what must stay cheap, and what must stay off the boot path.

These are behaviour tests, not benchmarks. A wall-clock assertion is not stable on CI, and
it does not show *why* a regression occurred. Instead, each test checks a structural
property on which the optimisation depends:

* The database opens in the mode that makes small writes cheap.
* MeshTerm skips the scans in each frame that cannot change a picture.
* The import graph at startup does not include the three subtrees that each added a fifth
  of a second to each run.

We measured the numbers for these choices on the PicoCalc (Luckfox Lyra, approximately
1 GHz Cortex-A7, SD card). They are in the Measurements appendix of the plan.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput

from meshterm.persistence import db
from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui.tui.session import _has_wide_glyph
from meshterm.ui.tui.spinner import Spinner

if TYPE_CHECKING:
    from meshterm.ui.tui.fastrender import FastRenderer

# --- the write path of the database -------------------------------------------------


def test_connect_opens_in_wal_mode(tmp_path: Path) -> None:
    """WAL makes each small insert an append. Without WAL, it is a rewrite of the journal."""
    conn = db.connect(tmp_path / "wal.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_relaxed_sync_is_only_taken_together_with_wal(tmp_path: Path) -> None:
    """MeshTerm sets ``synchronous=NORMAL`` only with WAL. It never sets one without the other.

    ``synchronous=NORMAL`` is safe with WAL. Without WAL, it can corrupt the file. With WAL,
    NORMAL can lose the last transactions if the power fails. With the rollback journal, it
    can destroy the file. The PicoCalc runs on a battery pack. Thus the two settings are
    together or not at all.
    """
    conn = db.connect(tmp_path / "sync.db")
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    if journal == "wal":
        assert synchronous == 1  # NORMAL
    else:  # pragma: no cover - only on a filesystem that refuses WAL
        assert synchronous == 2  # FULL, the safe default that MeshTerm does not change


def test_temp_store_is_memory(tmp_path: Path) -> None:
    """Sorts and temporary b-trees stay in RAM. They do not go to the SD card."""
    conn = db.connect(tmp_path / "temp.db")
    assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY


def test_tuning_survives_a_reopen(tmp_path: Path) -> None:
    """WAL is persistent, so a second session gets it, and the relaxed sync with it."""
    path = tmp_path / "reopen.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


# --- what must not be imported at startup -------------------------------------------

#: Subtrees that each take a fifth of a second to import on the Lyra, and that no startup
#: path needs. PyCryptodome probes the crypto features of the CPU (the import is only
#: through ``core.channels``, and the boot path needed its constants). The map screen
#: imports the tile downloader of the basemap with it: ``urllib.request`` →
#: ``http.client`` → ``ssl``.
_MUST_STAY_LAZY = ("Crypto", "urllib.request", "http.client", "meshterm.ui.map_screen")


def test_startup_does_not_import_the_heavy_optional_subtrees() -> None:
    """``import meshterm.cli`` does not import crypto, the network stack, or the map.

    The test runs in a subprocess, because the assertion is about the import graph of a
    *new* interpreter. When this suite runs, half of the code base is already in
    ``sys.modules``.
    """
    probe = (
        "import sys, meshterm.cli;"
        "print(','.join(sorted(m for m in sys.modules"
        f" if any(m == h or m.startswith(h + '.') for h in {_MUST_STAY_LAZY!r}))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"these should not load at startup: {out}"


def test_the_deferred_crypto_still_decrypts() -> None:
    """The deferred import must not remove the names that the function needs."""
    from meshterm.core.channels import decrypt_channel_text, identify_channel

    # No known channel matches. But if the function reaches the "no match" answer, HMAC and
    # AES were resolved.
    assert identify_channel("ab", "cdef", "00" * 16, [("#public", b"\x01" * 16)]) is None
    assert decrypt_channel_text("ab", "cdef", "00" * 16, [("#public", b"\x01" * 16)]) is None


def test_map_default_fraction_is_the_same_constant_from_either_module() -> None:
    """The map screen exports the geo constant again. Thus the existing importers see one value."""
    from meshterm.core.geo import DEFAULT_VIEW_FRACTION as from_geo
    from meshterm.ui.map_screen import DEFAULT_VIEW_FRACTION as from_screen

    assert from_geo == from_screen


def test_widgets_does_not_drag_the_spatial_modules() -> None:
    """``ui.widgets`` gets its mark constants from ``ui.marks``, not from the heavy modules.

    The P2 sweep measured approximately 60 ms of import time on the Lyra. ``widgets``
    imported ``map_render``, ``mapcanvas``, and ``pathgraph`` only for constants. In P3, we
    moved the constants to ``ui.marks``, which has no dependencies. This test uses the same
    subprocess method as the startup guard, because the claim is about the import graph of
    a new interpreter.
    """
    heavy = ("meshterm.ui.map_render", "meshterm.ui.mapcanvas", "meshterm.ui.pathgraph")
    probe = (
        "import sys, meshterm.ui.widgets;"
        f"print(','.join(sorted(m for m in sys.modules if m in {heavy!r})))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"widgets should not drag these anymore: {out}"


# --- work in each frame that the platform can skip ----------------------------------


def test_wide_glyph_scan_short_circuits_without_emoji() -> None:
    """A console font with no emoji has no wide glyph.

    Thus MeshTerm can skip the scan in each frame.
    """
    waving = "hi \U0001f44b"
    assert _has_wide_glyph(waving) is True
    set_platform(PICOCALC_LYRA)
    assert _has_wide_glyph(waving) is False


def test_spinner_cycle_follows_the_platform() -> None:
    """The spinner uses braille where effects are on.

    It uses the plain LINE cycle where effects are off.
    """
    assert Spinner().frames == Spinner.BRAILLE
    set_platform(PICOCALC_LYRA)
    assert Spinner().frames == Spinner.LINE


def test_an_explicit_spinner_cycle_still_wins() -> None:
    """The platform gives a *default*. A caller that gives its own animation steps keeps them."""
    set_platform(PICOCALC_LYRA)
    assert Spinner("ab").frames == "ab"


def test_battery_gauge_steps_the_sweep_at_the_platforms_own_cadence(monkeypatch) -> None:
    """The charging sweep runs on the PicoCalc too.

    It has one step for each paint, not for each second.
    """
    from meshterm.services.battery_service import BatteryReading
    from meshterm.ui import menu
    from meshterm.ui.menu import _battery_segment

    class _Ctx:
        class battery:  # noqa: N801 - a stand-in for the service, not a real class name
            @staticmethod
            def reading() -> BatteryReading:
                return BatteryReading(millivolts=3300, percent=5, charging=True)

    ctx = _Ctx()
    clock = 0.0
    monkeypatch.setattr(menu.time, "monotonic", lambda: clock)

    def _sweep(seconds: float) -> list[str]:
        """The fills of the gauge over five ticks of ``seconds`` each."""
        nonlocal clock
        out = []
        for step in range(5):
            clock = step * seconds
            out.append(_battery_segment(ctx).plain[0])  # type: ignore[arg-type]
        return out

    set_platform(PICOCALC_LYRA)
    # One step for each idle paint at 2 s: the sweep climbs, and it does not alias across
    # skipped frames.
    assert len(set(_sweep(PICOCALC_LYRA.tick_s))) == 5
    assert len(set(_sweep(PICOCALC_LYRA.tick_s / 2))) < 5  # half a tick apart: frames repeat
    set_platform(REGULAR)
    assert len(set(_sweep(REGULAR.tick_s))) == 5
    assert _battery_segment(ctx).plain.endswith("5%")  # type: ignore[arg-type]


def test_idle_and_spin_cadences_are_slower_where_a_frame_is_dear() -> None:
    """The PicoCalc composes a frame in approximately 74 ms.

    Thus both animation clocks must be slower. Suppose the PicoCalc used the rates of the
    desktop. A 1 Hz idle tick alone would use 7 to 11% of the core for no purpose. A spinner
    at 0.12 s would ask for more frames each second than the hardware can compose. Neither
    is a free preference: both must stay slower than the rate of regular.
    """
    assert REGULAR.tick_s == 1.0
    assert REGULAR.spinner_tick_s == 0.12
    assert PICOCALC_LYRA.tick_s > REGULAR.tick_s
    # A frame takes approximately 110 ms at the taller 53x40 size. A spin cadence that is
    # shorter than this would queue paints faster than they can finish.
    assert PICOCALC_LYRA.spinner_tick_s > 0.12


def test_spinner_interval_reads_the_active_platform() -> None:
    """There is one source for the spin cadence.

    MeshTerm reads it again at each tick, so it never misses ``set_platform``.
    """
    from meshterm.ui.tui.spinner import spinner_interval

    assert spinner_interval() == REGULAR.spinner_tick_s
    set_platform(PICOCALC_LYRA)
    assert spinner_interval() == PICOCALC_LYRA.spinner_tick_s


# --- the renderer that writes the changed rows directly -----------------------------


class _CapturingOutput(DummyOutput):
    """A DummyOutput that records what the renderer wrote, at a fixed console size."""

    def __init__(self, rows: int = 26, columns: int = 53) -> None:
        super().__init__()
        self.written: list[str] = []
        self._size = Size(rows=rows, columns=columns)

    def get_size(self) -> Size:
        return self._size

    def write_raw(self, data: str) -> None:
        self.written.append(data)

    def cursor_goto(self, row: int = 0, column: int = 0) -> None:
        self.written.append(f"<goto {row},{column}>")

    def erase_screen(self) -> None:
        self.written.append("<erase-screen>")

    @property
    def stream(self) -> str:
        return "".join(self.written)


def _fast_renderer(frames: list[str]) -> tuple[FastRenderer, _CapturingOutput]:
    """A FastRenderer that gets a scripted sequence of composed frames."""
    from prompt_toolkit.styles import Style

    from meshterm.ui.tui.fastrender import FastRenderer

    out = _CapturingOutput()
    pending = list(frames)
    renderer = FastRenderer(Style([]), out, full_screen=True, frame_source=lambda: pending.pop(0))
    return renderer, out


def test_fastrender_is_on_with_an_escape_hatch(monkeypatch) -> None:  # noqa: ANN001
    """The row writer is the default paint. Only an explicit 0 returns the paint to pt."""
    from meshterm.ui.tui import fastrender

    monkeypatch.delenv("MESHTERM_FASTRENDER", raising=False)
    assert fastrender.enabled()
    monkeypatch.setenv("MESHTERM_FASTRENDER", "0")
    assert not fastrender.enabled()


def test_fastrender_rewrites_only_the_rows_that_changed() -> None:
    """A key press that changes one row must not paint the whole terminal.

    This is the purpose of the renderer.
    """
    rows_a = [f"row {i:02d}" for i in range(26)]
    rows_b = list(rows_a)
    rows_b[7] = "row 07 SELECTED"
    renderer, out = _fast_renderer(["\n".join(rows_a), "\n".join(rows_b)])

    renderer.render(None, None)  # first paint: all rows
    out.written.clear()
    renderer.render(None, None)  # second paint: one row changed

    stream = out.stream
    assert "row 07 SELECTED" in stream
    # The text of each *other* row is not in the output.
    assert "row 06" not in stream
    assert "row 08" not in stream
    assert "\x1b[8;1H" in stream  # the renderer addressed row 8 (1-based) directly


def test_fastrender_erases_before_it_draws_so_a_full_row_keeps_its_last_cell() -> None:
    """An erase-to-end *after* a full-width row removes the character that the renderer just wrote.

    A row that fills the console exactly leaves the cursor in the last column with a wrap
    pending. An erase there clears that cell, and the last glyph of the row is missing (it
    was missing on the 53 columns of the PicoCalc). Thus the erase must come first. It must
    never come after the row.
    """
    full = "x" * 53
    renderer, out = _fast_renderer(["\n".join([full] * 26), "\n".join([full] * 26)])
    renderer.render(None, None)
    stream = out.stream
    assert "\x1b[K" in stream
    # In each row, the clear is before the text of the row, and no clear is after it.
    for chunk in stream.split("\x1b[0m")[1:]:
        assert chunk.startswith("\x1b[K"), chunk[:40]
        assert not chunk.rstrip().endswith("\x1b[K"), chunk[-40:]


def test_fastrender_hands_a_frame_it_cannot_place_back_to_prompt_toolkit(
    monkeypatch,  # noqa: ANN001
) -> None:
    """A ``None`` frame means that the session did not place it. Then pt must do that paint.

    The session answers ``None`` for the frames that it does not lay out itself. These are
    the busy overlay (the float container measures it and centres it as a window that has
    the size of its content) and an empty stack (it has no background).
    """
    from prompt_toolkit.renderer import Renderer
    from prompt_toolkit.styles import Style

    from meshterm.ui.tui.fastrender import FastRenderer

    calls: list[bool] = []
    monkeypatch.setattr(Renderer, "render", lambda *a, **k: calls.append(True))
    renderer = FastRenderer(
        Style([]), _CapturingOutput(), full_screen=True, frame_source=lambda: None
    )
    renderer.render(None, None)
    assert calls == [True]
    assert renderer.slow_paints == 1
    assert renderer.fast_paints == 0


def test_fastrender_writes_nothing_at_all_when_the_frame_is_unchanged() -> None:
    """The frame of the idle tick is usually the same, and the same frame must cost no bytes.

    It is not enough to write fewer bytes. An empty write leaves the damage region of the
    display empty. Thus the display does not flush, and the SPI bus is quiet between real
    changes.
    """
    same = "\n".join(f"row {i:02d}" for i in range(26))
    renderer, out = _fast_renderer([same, same])

    renderer.render(None, None)  # first paint: all rows
    out.written.clear()
    renderer.render(None, None)  # the tick: nothing changed

    assert out.written == []
    assert renderer.fast_paints == 2


# --- dialogs stay on the fast path ---------------------------------------------------


def test_a_dialog_is_composited_onto_the_frame_rather_than_handed_to_prompt_toolkit() -> None:
    """MeshTerm puts a dialog on the frame. It does not give the frame to prompt_toolkit.

    A float once sent the whole frame to the renderer of pt, and MeshTerm paid for the
    rebuild of its grid. MeshTerm can reproduce the placement: a float that has no anchor
    and no size is centred on the frame. Thus MeshTerm merges the box here, and the row diff
    still applies. The rows that the box does not reach are the *same strings*, and this
    lets the diff skip them.
    """
    from meshterm.ui.tui.frame import composite_float

    base = [f"{i:02d}" + "." * 51 for i in range(26)]
    box = "\n".join(["+" + "-" * 18 + "+"] + ["|" + " " * 18 + "|"] * 4 + ["+" + "-" * 18 + "+"])
    out = composite_float(base, box, 53, 26)

    assert len(out) == 26
    # The box has six rows, centred vertically: (26 - 6) // 2 = 10.
    for i in list(range(10)) + list(range(16, 26)):
        assert out[i] is base[i], f"row {i} was rewritten for nothing"
    # The box is also centred horizontally: (53 - 20) // 2 = 16 cells of the base stay on
    # the left.
    plain = _plain_row(out[10])
    assert plain.startswith("10" + "." * 14)
    assert plain[16:36] == "+" + "-" * 18 + "+"
    assert plain[36:].startswith(".")
    assert len(plain) == 53


def test_a_composited_row_keeps_the_styling_of_both_sides() -> None:
    """The merge is exact to the cell over *styled* rows. The colour of the base must stay."""
    from rich.text import Text

    from meshterm.ui.tui.frame import composite_float
    from meshterm.ui.tui.render import render_to_ansi

    base = [render_to_ansi(Text("x" * 53, style="bold red"), 53)] * 10
    box = render_to_ansi(Text("[ ok ]", style="bold green"), 6)
    out = composite_float(base, box, 53, 10)

    row = out[(10 - 1) // 2]
    assert "[ ok ]" in _plain_row(row)
    assert row.count("\x1b[") > 2, "styling was flattened by the merge"


def test_stacked_dialogs_composite_in_z_order() -> None:
    """A confirm over a picker over a menu: each box goes on the frame below it."""
    from meshterm.ui.tui.frame import composite_float

    rows = ["." * 53 for _ in range(26)]
    rows = composite_float(rows, "\n".join(["under" + "." * 15] * 8), 53, 26)
    rows = composite_float(rows, "\n".join(["OVER"] * 2), 53, 26)

    middle = _plain_row(rows[12])
    # The lower box is 20 cells wide, so it starts at (53 - 20) // 2 = 16. The box of 4
    # cells above it starts at 24. It covers the middle of the lower box and does not change
    # its edges.
    assert middle.startswith("." * 16 + "under")
    assert middle[24:28] == "OVER"


def _plain_row(row: str) -> str:
    """The text of the row, without its ANSI escape sequences."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", row)
