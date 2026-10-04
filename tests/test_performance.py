# SPDX-License-Identifier: Apache-2.0
"""The P2 performance contracts: what must stay cheap, and what must stay off the boot path.

These are behaviour tests, not benchmarks — a wall-clock assertion would be flaky on CI and
would say nothing about *why* a regression happened. Each test instead pins the structural
property the optimisation rests on: the database opens in the mode that makes small writes
cheap, the per-frame scans that cannot change a picture are skipped, and the startup import
graph stays clear of the three subtrees that were dragging a fifth of a second each onto every
run.

The numbers behind these choices were measured on the PicoCalc (Luckfox Lyra, ~1 GHz
Cortex-A7, SD card) and are recorded in the plan's Measurements appendix.
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

# --- the database's write path ------------------------------------------------------


def test_connect_opens_in_wal_mode(tmp_path: Path) -> None:
    """WAL is what turns each tiny insert into an append instead of a journal rewrite."""
    conn = db.connect(tmp_path / "wal.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_relaxed_sync_is_only_taken_together_with_wal(tmp_path: Path) -> None:
    """``synchronous=NORMAL`` is safe under WAL and corrupting without it — never split.

    Under WAL, NORMAL risks losing the last transactions to a power cut; under the rollback
    journal it risks the file itself. The PicoCalc runs on a battery pack, so the two
    settings travel together or not at all.
    """
    conn = db.connect(tmp_path / "sync.db")
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    if journal == "wal":
        assert synchronous == 1  # NORMAL
    else:  # pragma: no cover - only on a filesystem that refuses WAL
        assert synchronous == 2  # FULL, the safe default left untouched


def test_temp_store_is_memory(tmp_path: Path) -> None:
    """Sorts and temporary b-trees stay in RAM rather than landing on the SD card."""
    conn = db.connect(tmp_path / "temp.db")
    assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY


def test_tuning_survives_a_reopen(tmp_path: Path) -> None:
    """WAL is persistent, so a second session inherits it — and with it the relaxed sync."""
    path = tmp_path / "reopen.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


# --- what must not be imported at startup -------------------------------------------

#: Subtrees that cost a fifth of a second *each* to import on the Lyra and are needed by no
#: startup path: PyCryptodome probes the CPU's crypto features (reached only through
#: ``core.channels``, whose constants the boot path wanted), and the map screen drags the
#: basemap's tile fetcher — ``urllib.request`` → ``http.client`` → ``ssl`` — behind it.
_MUST_STAY_LAZY = ("Crypto", "urllib.request", "http.client", "meshterm.ui.map_screen")


def test_startup_does_not_import_the_heavy_optional_subtrees() -> None:
    """``import meshterm.cli`` stays clear of crypto, the network stack, and the map.

    Run in a subprocess because the assertion is about a *fresh* interpreter's import graph:
    by the time this suite is running, half the codebase is already in ``sys.modules``.
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
    """Deferring the import must not have cost the function its names."""
    from meshterm.core.channels import decrypt_channel_text, identify_channel

    # No known channel matches, but reaching the "no match" answer means HMAC/AES resolved.
    assert identify_channel("ab", "cdef", "00" * 16, [("#public", b"\x01" * 16)]) is None
    assert decrypt_channel_text("ab", "cdef", "00" * 16, [("#public", b"\x01" * 16)]) is None


def test_map_default_fraction_is_the_same_constant_from_either_module() -> None:
    """The map screen re-exports the geo constant, so existing importers still see one value."""
    from meshterm.core.geo import DEFAULT_VIEW_FRACTION as from_geo
    from meshterm.ui.map_screen import DEFAULT_VIEW_FRACTION as from_screen

    assert from_geo == from_screen


def test_widgets_does_not_drag_the_spatial_modules() -> None:
    """``ui.widgets`` reads its mark constants from ``ui.marks``, not the heavy hosts.

    The P2 sweep measured ~60 ms of Lyra import time in ``widgets`` pulling
    ``map_render``/``mapcanvas``/``pathgraph`` for constants alone; the constants moved
    to the dependency-free ``ui.marks`` in P3. Same subprocess trick as the startup
    guard: the claim is about a fresh interpreter's import graph.
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


# --- per-frame work the platform can skip -------------------------------------------


def test_wide_glyph_scan_short_circuits_without_emoji() -> None:
    """A no-emoji console font has no wide glyph, so the per-frame scan can be skipped."""
    waving = "hi \U0001f44b"
    assert _has_wide_glyph(waving) is True
    set_platform(PICOCALC_LYRA)
    assert _has_wide_glyph(waving) is False


def test_spinner_cycle_follows_the_platform() -> None:
    """Braille where effects are on, the plain LINE cycle where they aren't."""
    assert Spinner().frames == Spinner.BRAILLE
    set_platform(PICOCALC_LYRA)
    assert Spinner().frames == Spinner.LINE


def test_an_explicit_spinner_cycle_still_wins() -> None:
    """The platform supplies a *default*; a caller that names its frames keeps them."""
    set_platform(PICOCALC_LYRA)
    assert Spinner("ab").frames == "ab"


def test_battery_gauge_steps_the_sweep_at_the_platforms_own_cadence(monkeypatch) -> None:
    """The charging sweep runs on the PicoCalc too — a step per repaint, not per second."""
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
        """The gauge's fills over five ticks of ``seconds`` each."""
        nonlocal clock
        out = []
        for step in range(5):
            clock = step * seconds
            out.append(_battery_segment(ctx).plain[0])  # type: ignore[arg-type]
        return out

    set_platform(PICOCALC_LYRA)
    # A step per 2 s idle repaint: the sweep climbs, and does not alias across skipped frames.
    assert len(set(_sweep(PICOCALC_LYRA.tick_s))) == 5
    assert len(set(_sweep(PICOCALC_LYRA.tick_s / 2))) < 5  # half a tick apart, some frames repeat
    set_platform(REGULAR)
    assert len(set(_sweep(REGULAR.tick_s))) == 5
    assert _battery_segment(ctx).plain.endswith("5%")  # type: ignore[arg-type]


def test_idle_and_spin_cadences_are_slower_where_a_frame_is_dear() -> None:
    """PicoCalc composes a frame in ~74 ms, so both animation clocks have to give.

    At the desktop's rates a 1 Hz idle tick alone would spend 7-11% of the core doing
    nothing, and a 0.12 s spinner would ask for more frames per second than the hardware
    can physically compose. Neither is a free-floating preference: both must stay strictly
    slower than regular's.
    """
    assert REGULAR.tick_s == 1.0
    assert REGULAR.spinner_tick_s == 0.12
    assert PICOCALC_LYRA.tick_s > REGULAR.tick_s
    # A frame costs ~110 ms at the taller 53x40 geometry; a spin cadence under that would
    # queue repaints faster than they can finish.
    assert PICOCALC_LYRA.spinner_tick_s > 0.12


def test_spinner_interval_reads_the_active_platform() -> None:
    """One source for the spin cadence, re-read per tick so set_platform is never missed."""
    from meshterm.ui.tui.spinner import spinner_interval

    assert spinner_interval() == REGULAR.spinner_tick_s
    set_platform(PICOCALC_LYRA)
    assert spinner_interval() == PICOCALC_LYRA.spinner_tick_s


# --- the direct row-diff renderer ---------------------------------------------------


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
    """A FastRenderer fed a scripted sequence of composed frames."""
    from prompt_toolkit.styles import Style

    from meshterm.ui.tui.fastrender import FastRenderer

    out = _CapturingOutput()
    pending = list(frames)
    renderer = FastRenderer(Style([]), out, full_screen=True, frame_source=lambda: pending.pop(0))
    return renderer, out


def test_fastrender_is_on_with_an_escape_hatch(monkeypatch) -> None:  # noqa: ANN001
    """The row writer is the default paint; only an explicit 0 hands it back to pt."""
    from meshterm.ui.tui import fastrender

    monkeypatch.delenv("MESHTERM_FASTRENDER", raising=False)
    assert fastrender.enabled()
    monkeypatch.setenv("MESHTERM_FASTRENDER", "0")
    assert not fastrender.enabled()


def test_fastrender_rewrites_only_the_rows_that_changed() -> None:
    """The whole point: a keystroke that moves one row must not repaint the screen."""
    rows_a = [f"row {i:02d}" for i in range(26)]
    rows_b = list(rows_a)
    rows_b[7] = "row 07 SELECTED"
    renderer, out = _fast_renderer(["\n".join(rows_a), "\n".join(rows_b)])

    renderer.render(None, None)  # first paint: everything
    out.written.clear()
    renderer.render(None, None)  # second: one row moved

    stream = out.stream
    assert "row 07 SELECTED" in stream
    # Every *other* row's text stayed off the wire.
    assert "row 06" not in stream
    assert "row 08" not in stream
    assert "\x1b[8;1H" in stream  # addressed row 8 (1-based) directly


def test_fastrender_erases_before_it_draws_so_a_full_row_keeps_its_last_cell() -> None:
    """Erase-to-end *after* a full-width row wipes the character just written.

    A row that exactly fills the console leaves the cursor in the last column with wrap
    pending; an erase there clears that cell, and the row's final glyph goes missing (it
    did, on the PicoCalc's 53 columns). So the erase has to lead, never follow.
    """
    full = "x" * 53
    renderer, out = _fast_renderer(["\n".join([full] * 26), "\n".join([full] * 26)])
    renderer.render(None, None)
    stream = out.stream
    assert "\x1b[K" in stream
    # In every row the clear precedes that row's text, and no clear trails it.
    for chunk in stream.split("\x1b[0m")[1:]:
        assert chunk.startswith("\x1b[K"), chunk[:40]
        assert not chunk.rstrip().endswith("\x1b[K"), chunk[-40:]


def test_fastrender_hands_a_frame_it_cannot_place_back_to_prompt_toolkit(
    monkeypatch,  # noqa: ANN001
) -> None:
    """A ``None`` frame means the session declined to place it — pt must own that paint.

    The session answers ``None`` for the frames it does not lay out itself: the busy
    overlay, which the float container measures and centres as a content-sized window, and
    an empty stack, which has no background at all.
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
    """The idle tick's frame is usually identical, and identical must cost no bytes.

    Not merely fewer bytes: an empty write leaves the panel's damage region empty, so the
    display never flushes and the SPI bus stays quiet between real changes.
    """
    same = "\n".join(f"row {i:02d}" for i in range(26))
    renderer, out = _fast_renderer([same, same])

    renderer.render(None, None)  # first paint: everything
    out.written.clear()
    renderer.render(None, None)  # the tick: nothing moved

    assert out.written == []
    assert renderer.fast_paints == 2


# --- dialogs stay on the fast path ---------------------------------------------------


def test_a_dialog_is_composited_onto_the_frame_rather_than_handed_to_prompt_toolkit() -> None:
    """A float used to drop the whole frame onto pt's renderer — and pay its grid rebuild.

    The placement is reproducible: an unanchored float with no size of its own is centred
    on the frame. So the box is merged in here, the row diff still applies, and the rows
    the box does not reach come back as the *same strings* — which is what lets the diff
    skip them.
    """
    from meshterm.ui.tui.frame import composite_float

    base = [f"{i:02d}" + "." * 51 for i in range(26)]
    box = "\n".join(["+" + "-" * 18 + "+"] + ["|" + " " * 18 + "|"] * 4 + ["+" + "-" * 18 + "+"])
    out = composite_float(base, box, 53, 26)

    assert len(out) == 26
    # Six box rows, centred vertically: (26 - 6) // 2 = 10.
    for i in list(range(10)) + list(range(16, 26)):
        assert out[i] is base[i], f"row {i} was rewritten for nothing"
    # Centred horizontally too: (53 - 20) // 2 = 16 cells of the base still on the left.
    plain = _plain_row(out[10])
    assert plain.startswith("10" + "." * 14)
    assert plain[16:36] == "+" + "-" * 18 + "+"
    assert plain[36:].startswith(".")
    assert len(plain) == 53


def test_a_composited_row_keeps_the_styling_of_both_sides() -> None:
    """The merge is cell-accurate over *styled* rows — the base's colour must survive it."""
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
    """A confirm over a picker over a menu: each box lands on the frame below it."""
    from meshterm.ui.tui.frame import composite_float

    rows = ["." * 53 for _ in range(26)]
    rows = composite_float(rows, "\n".join(["under" + "." * 15] * 8), 53, 26)
    rows = composite_float(rows, "\n".join(["OVER"] * 2), 53, 26)

    middle = _plain_row(rows[12])
    # The lower box is 20 cells wide, so it starts at (53 - 20) // 2 = 16; the 4-cell box
    # above it starts at 24 and covers the lower one's middle without disturbing its edges.
    assert middle.startswith("." * 16 + "under")
    assert middle[24:28] == "OVER"


def _plain_row(row: str) -> str:
    """The row's text with its ANSI escapes stripped."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", row)
