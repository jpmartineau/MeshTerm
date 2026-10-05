# SPDX-License-Identifier: Apache-2.0
"""The platform seam: a frozen spec every consumer binds to once, at boot.

MeshTerm runs its flavours from one codebase — the regular desktop terminal, the
PicoCalc's 53-column framebuffer console, and the Cardputer Zero's 53×14 panel — with no
forked screens and no runtime layer.
A :class:`Platform` is resolved once, at process start, and held in a module-level
singleton; everything downstream (the theme, the frame compositor, the header, the
session's width handling) reads it at *its own* construction/call time and binds its
behaviour accordingly, so the render loop itself never branches on platform.

Read the active platform through :func:`get_platform` (or the module-qualified
``platforms.PLATFORM``) — never ``from meshterm.platforms import PLATFORM`` at a
module's top level. That statement runs once, at import time, and copies whatever
:data:`PLATFORM` pointed to *then*; :func:`set_platform` rebinding the module global
afterwards would never reach that stale copy. A function call always re-reads the
current value, so it has no such trap.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Platform:
    """One flavour's complete rendering/behaviour spec.

    Every field is data a consumer binds to at its own construction/call time (see the
    module docstring) — there is no code path that branches on ``platform.name`` itself
    outside this module and the small set of binding points documented at each field.

    Attributes:
        name: Stable identifier — ``"regular"``, ``"picocalc-lyra"`` or ``"cardputer-zero"``
            — used for ``--platform``/``MESHTERM_PLATFORM`` matching and diagnostics. A
            handheld's is its product and the variant that sets it apart from its siblings:
            M5Stack makes three Cardputers and only the Zero runs Linux, and the PicoCalc
            takes several cores and only the Luckfox Lyra runs MeshTerm.
        readable_cols: The readability standard (CLAUDE.md's "screens stay readable at
            N columns") and the dual-platform gallery's test width.
        readable_rows: The row *floor* a screen designs to. PicoCalc's live console is
            53×26 (custom 6×12 font); a 6×8 font rebuild (P5) gives 53×40. Screens design
            to this floor and exploit extra rows fluidly; the gallery renders picocalc-lyra at
            both 26 and 40 rows.
        frame_border: Whether the base screen draws inside a bordered ``Panel``. ``False``
            swaps it for a title-bar row instead (+4 cols, +1 row reclaimed) — a PicoCalc
            chrome saving, wired in P4.
        menu_icons: Whether a *command row* leads with a decorative icon — the main menu's
            per-tool mark, an action list's ``🗑``/``🕒``, a body action's ``✎``. The label
            already names the action, so the icon is a family cue, not information; where
            the console can only spell it as a one-glyph stand-in the cue is worth less
            than the cells it costs and reads as noise besides (``@`` for both Map and
            Mesh walk, ``…`` for both Live feed and Time machine — JP, on-device,
            2026-08-11). ``False`` drops the whole lane, mark and emoji alike, and the
            cells go to the label and its description. This is about *decoration* only:
            a glyph that carries data — a channel's openness, a packet's class, a node's
            type, a status mark on an outcome — is not a menu icon and is never dropped
            (see :func:`~meshterm.ui.menus.command_label`).
        dialog_margin: Total columns a floating dialog leaves to the backdrop it sits over
            — half of it on each side (see
            :func:`~meshterm.ui.tui.frame._dialog_layout`). The gutter is what makes a
            dialog read as floating rather than as a new screen, and three columns a side
            do that comfortably at 72. On the 53-column console the *same* gutter is a
            twelfth of the whole display, and it costs content: a packet card's reception
            row folds ``rssi`` onto a line of its own for want of two cells (JP, on-device,
            2026-08-10). Two columns a side still reads as floating and buys them back.
        dialog_row_margin: Rows a floating dialog leaves to the frame around it — the header
            above and the footer below, plus any blank row between them and the box. One row
            of air above and below is what lets a box read as floating on a 24-row terminal
            (4 in all). On the Cardputer's 14 rows every row a dialog gives up is a row it
            can't show, its side gutters already say it floats, and there is no header row
            above the title bar, so a tall box runs from the top row to the lane (1),
            drawing over the bar — whose ends either side of the box are blanked rather than
            left as fragments (see :func:`~meshterm.ui.tui.frame.composite_float`). Never
            over the lane: a dialog's lane is the dialog's own, and the keys on it are named
            nowhere else.
        header_atoms: Which segments compose the persistent header, in the vocabulary
            ``"version"``, ``"device"``, ``"badges"``, ``"pulse"``, ``"battery"``. Wired
            into :func:`~meshterm.ui.menu._header` in P4; unconsumed until then.
        header_row: Whether the persistent header has a row of its own above the frame.
            Where it doesn't (the Cardputer, JP 2026-10-03), the header's atoms ride the
            borderless title bar's right end, after the way out — so its badges and battery
            sit in the top-right corner they always had, one row up, and every screen gets
            that row back — and the wordmark becomes the main menu's title (see
            :func:`~meshterm.ui.menu._menu_title`). Only a borderless frame can fold it: the
            bordered panel has no bar to carry it.
        lane_deck: Which F-key lane deck the footer deals, by name (see
            :data:`~meshterm.ui.tui.fkeys.DECKS`), or ``""`` where the footer is each
            screen's own ``footer_hint`` string instead. A deck is everything about the lane
            that belongs to one keyboard: which keycodes drive its slots, where its chips sit
            on the row, how they are drawn, and which of a screen's lane definitions it reads
            (``Screen.picocalc_lyra_lane``, ``Screen.cardputer_zero_lane``). Each handheld names its
            own, so one can change without the other following. :attr:`footer_fkeys` is the
            yes/no reading of it.
        width_reclaim: Whether the session may report one extra terminal column (see
            :class:`~meshterm.ui.tui.session._WidthExtendedOutput`). ``True`` only *lets*
            it: the column is reclaimed where the terminal's size probe hid one, which is
            prompt_toolkit's Windows console output and nothing else
            (:func:`~meshterm.ui.tui.session._probe_hides_last_column`). ``False`` on
            PicoCalc's exact-width console, where a phantom extra column tears the frame.
            ``MESHTERM_FULL_WIDTH`` remains an explicit override on top of this default.
        emoji: Whether emoji icons render at all. ``False`` means every icon funnel routes
            through a compact single-BMP-glyph table instead (P3), and neither half of the
            emoji alignment runs — the reserve-two measurement
            (:func:`~meshterm.ui.tui.emoji_width.install`) or the column pinning
            (:mod:`~meshterm.ui.tui.colsnap`). Once no emoji are ever drawn, every glyph on
            screen is one the console font has verified, and the stock widths are exact.
        font: The glyph inventory the screen is drawn in, by name (see
            :data:`~meshterm.ui.fontset.FONTS`), or ``""`` where a terminal draws whatever
            it is sent. A handheld's every frame is folded down to its font at the render
            boundary (:func:`~meshterm.ui.theme.fold_text`) — an accent the font lacks
            stripped, an emoji turned to its compact glyph, anything else still missing a
            narrow ``?`` — so nothing the panel can't draw is ever sent to it. Storage is
            never touched.
        truecolor: Whether the theme may use arbitrary 24-bit SGR colour. ``False`` selects
            a 16-slot palette theme instead (the console's real ceiling — no per-cell RGB),
            and quantizes the two scales that would otherwise spend a gradient — the
            per-node key hue (:func:`~meshterm.ui.theme.node_style`) and the heard-age heat
            (:func:`~meshterm.ui.widgets._recency_style`). Wired in P3.
        solid_braille: Whether the console font draws a braille cell's eight dots as solid
            tiles with no gap between them or between neighbouring cells — the PicoCalc's
            built fonts (``scripts/picocalc-lyra``) do — so braille is a true 2×4 pixel grid,
            fine enough for what must stay contiguous. It is what lets every QR code be
            drawn in braille, one module a dot (:mod:`~meshterm.ui.qr`). A desktop font's
            braille is dotted, and a code drawn in it scans as nothing.
        url_codes: Whether a chat message's URLs are drawn as QR codes under it, side by
            side (:meth:`~meshterm.ui.chat.ChatScreen._body_lines`) — so a link read on the
            handheld opens on a phone without being typed out. The PicoCalc's alone: it
            takes braille codes (:attr:`solid_braille`) to be small enough to sit in a
            transcript at all, a quarter the area of the half blocks a desktop draws, and
            it takes rows — a short link's code is 9 of them, which the Cardputer's 14
            cannot spare under every message that carries one (JP, 2026-10-01).
        effects: Whether animated/decorative rendering runs at all — the braille spinner,
            which drops to a ``LINE`` fallback. Cheaper on a console where a repaint is
            dear. The header battery gauge is deliberately *not* behind it: its sweep is
            the charging state itself and its blink is the last warning before the pack
            dies, and both ride the idle repaint that happens anyway. Wired in P2/P3.
        tick_s: The :class:`~prompt_toolkit.application.Application` refresh interval — the
            *idle* repaint cadence only, since every screen pushes its own repaint through
            ``TuiSession.invalidate`` when its data actually moves. Composing one frame was
            measured at 74 ms on the PicoCalc at 53×26 (110 ms at 53×40), so a 1 Hz idle
            tick alone costs 7–11% of a core doing nothing; it ticks at half that rate
            there. What drifts is cosmetic — the header's per-minute pulse and its packet
            counter — and none of it is legible at one-second resolution anyway.
        spinner_tick_s: Seconds between frames of a "working" spinner. The single source for
            every animated wait in the app. The desktop's 0.12 s is not a rate this hardware
            can hold: a PicoCalc frame would claim 62% of a core at 53×26, and at 53×40 its
            110 ms of compose would leave under 10 ms of each interval for the work actually
            being waited on — and before this phase's savings it did not fit at all, at
            152 ms against a 120 ms budget. It slows to a cadence the hardware can hold
            comfortably while still reading as alive.
        battery: Which source feeds the header's battery gauge — ``"companion"`` (read
            from the connected MeshCore device) or ``"host"`` (the PicoCalc's own sysfs
            ``power_supply`` driver, confirmed present in P0). Wired in P5.
        own_display: Whether MeshTerm draws this display itself (:mod:`meshterm.emulator`)
            rather than running in a terminal — where the app is its own terminal, nothing
            about the one it was started from (a classic Windows console to move out of, a
            font to offer) is its business. True on the Cardputer Zero, whose launcher gives
            an app no console; and for any platform while the emulator shows it in a window.
        modifier_watch: The keyboard whose Shift state the optional evdev watcher follows,
            flipping the displayed F-key lane live while Shift is held — a case-insensitive
            substring of the input device's name, or ``""`` to leave the watcher off.
            Handhelds only: desktop terminals have no lane to flip, and the watcher needs
            ``/dev/input`` access a handheld's deploy user has (the ``input`` group). Always
            optional and lazy; wired in P4.
    """

    name: str
    readable_cols: int
    readable_rows: int
    frame_border: bool
    menu_icons: bool
    dialog_margin: int
    dialog_row_margin: int
    header_atoms: tuple[str, ...]
    header_row: bool
    lane_deck: str
    width_reclaim: bool
    emoji: bool
    font: str
    truecolor: bool
    solid_braille: bool
    url_codes: bool
    effects: bool
    tick_s: float
    spinner_tick_s: float
    battery: str
    own_display: bool
    modifier_watch: str

    @property
    def footer_fkeys(self) -> bool:
        """Whether the footer is an F-key lane rather than each screen's hint string."""
        return bool(self.lane_deck)


#: Exactly today's behaviour — the desktop/ssh terminal, unchanged by this seam's arrival.
REGULAR = Platform(
    name="regular",
    readable_cols=72,
    readable_rows=24,
    frame_border=True,
    menu_icons=True,
    dialog_margin=6,
    dialog_row_margin=4,
    header_atoms=("version", "device", "badges", "pulse", "battery"),
    header_row=True,
    lane_deck="",
    width_reclaim=True,
    emoji=True,
    font="",
    truecolor=True,
    solid_braille=False,
    url_codes=False,
    effects=True,
    tick_s=1.0,
    spinner_tick_s=0.12,
    battery="companion",
    own_display=False,
    modifier_watch="",
)

#: The PicoCalc/Lyra/Calculinux framebuffer console. Field values not yet consumed by a
#: binding point (header_atoms, font, truecolor, effects beyond the
#: two P1 bindings, tick_s, battery, modifier_watch) are P2–P5's targets, recorded here
#: now so the seam exists before the flavour work lands.
PICOCALC_LYRA = Platform(
    name="picocalc-lyra",
    readable_cols=53,
    readable_rows=26,
    frame_border=False,
    menu_icons=False,
    dialog_margin=4,
    dialog_row_margin=4,
    # JP's call (2026-08-01, post-P7 review): the handheld's header brands the app —
    # "MeshTerm vX" — rather than naming the device/port (on a soldered radio the port
    # never changes), and the activity pulse takes whatever room remains.
    header_atoms=("version", "badges", "pulse", "battery"),
    header_row=True,
    lane_deck="picocalc-lyra",
    width_reclaim=False,
    emoji=False,
    font="picocalc-lyra",
    truecolor=False,
    solid_braille=True,
    url_codes=True,
    effects=False,
    tick_s=2.0,
    spinner_tick_s=0.5,
    battery="host",
    own_display=False,
    modifier_watch="picocalc",
)

#: M5Stack's Cardputer Zero: a 320×170 panel drawn in 6×12 cells (53×14), under a
#: 46-key keyboard whose number keys 4–8 sit right below the display. Hardware not yet in
#: hand (2026-10-04), so this is chosen by ``--platform cardputer-zero`` / ``MESHTERM_PLATFORM``
#: only — no device-tree auto-detection until the device reports its own model string.
#:
#: The same 53 columns as the PicoCalc, which is why most of its flavour carries over; the
#: new constraint is rows. It has its own F-key lane deck (Fn+4…8, chips centred over their
#: keys), dealing the PicoCalc's lanes for now (JP, 2026-09-30). MeshTerm draws the panel
#: itself in RGB565 (:mod:`meshterm.emulator`), so it is not held to a 16-slot palette
#: (``truecolor``). Provisional until measured on the device: the cadences, taken from the
#: PicoCalc; and ``effects`` off. The battery is the handheld's own BQ27220 gauge, which the
#: kernel publishes as the ``power_supply`` ``bq27220-0`` (seen on the device, 2026-10-05) —
#: the pack the reader is holding, whichever radio is connected, as on the PicoCalc.
CARDPUTER_ZERO = Platform(
    name="cardputer-zero",
    readable_cols=53,
    readable_rows=14,
    frame_border=False,
    menu_icons=False,
    dialog_margin=4,
    dialog_row_margin=1,
    # No header row (JP, 2026-10-03): the badges and battery ride the title bar's right
    # end, the wordmark is the main menu's title, and the pulse, which only ever had the
    # header's leftover cells, goes — the dashboard draws the mesh's activity in full.
    header_atoms=("badges", "battery"),
    header_row=False,
    lane_deck="cardputer-zero",
    width_reclaim=False,
    emoji=False,
    font="cardputer-zero",
    truecolor=True,
    # The emulator draws its own braille, as the PicoCalc's built fonts do: solid
    # tiles, no gap between dots (meshterm/emulator/font.py, BRAILLE).
    solid_braille=True,
    url_codes=False,
    effects=False,
    tick_s=2.0,
    spinner_tick_s=0.5,
    battery="host",
    own_display=True,
    modifier_watch="tca8418c",
)

_BY_NAME = {p.name: p for p in (REGULAR, PICOCALC_LYRA, CARDPUTER_ZERO)}

#: The active platform. Do not import this name directly (see the module docstring) —
#: read it through :func:`get_platform`, and change it only through :func:`set_platform`.
PLATFORM = REGULAR


def get_platform() -> Platform:
    """Return the active :class:`Platform`.

    Safe to call from anywhere, at any time — always reflects the most recent
    :func:`set_platform` call, however that module was imported.
    """
    return PLATFORM


#: Callbacks re-run on every :func:`set_platform`, so a module can bind platform-derived
#: state (a chosen theme, a swapped function implementation) once per *switch* instead of
#: re-deriving it per call in a render loop. Registered via :func:`on_platform`.
_BINDINGS: list[Callable[[Platform], None]] = []


def on_platform(binding: Callable[[Platform], None]) -> Callable[[Platform], None]:
    """Register (and immediately run) a platform binding.

    The seam's principle is *bind at construction time, never per frame*: a consumer that
    must swap behaviour with the platform (the theme's ``name_style`` impl, the
    rasterizer's console cache) registers a binding here at its own import time. The
    binding runs once right away — against the platform active *now* — and again on every
    later :func:`set_platform`, so tests that swap platforms rebind automatically and the
    consumer's hot path reads a plain module global.

    Args:
        binding: Called with the active :class:`Platform`; must be idempotent.

    Returns:
        ``binding`` unchanged, so it can be used as a decorator.
    """
    _BINDINGS.append(binding)
    binding(PLATFORM)
    return binding


def set_platform(platform: Platform) -> None:
    """Install ``platform`` as the active one and re-run every registered binding.

    Called exactly once by the CLI callback, before :func:`~meshterm.ui.theme.make_console`
    — every other read of :func:`get_platform` in the same process happens after this.
    Idempotent (setting the same platform twice is a no-op in effect) and reversible, so
    tests can swap in a platform for one test and restore the previous one afterwards
    (see the ``_reset_platform`` autouse fixture in ``tests/conftest.py``).

    Args:
        platform: The platform to make active.
    """
    global PLATFORM
    PLATFORM = platform
    for binding in _BINDINGS:
        binding(platform)


@dataclass(frozen=True, slots=True)
class Resolution:
    """What :func:`resolve` looked at and what it landed on.

    Exists so the ``meshterm platform`` diagnostic can report every input the resolution
    considered, not just the final answer.

    Attributes:
        flag: The ``--platform`` value passed on the command line, if any.
        env: The ``MESHTERM_PLATFORM`` environment variable's value, if set.
        detected_model: The ``/proc/device-tree/model`` contents read during resolution
            (trailing NUL/space stripped), or ``None`` when the file doesn't exist (every
            non-Lyra machine, including every desktop and CI runner).
        source: Which input decided the outcome — ``"--platform flag"``,
            ``"MESHTERM_PLATFORM env var"``, ``"auto-detect"``, or ``"default"``.
        platform: The resolved :class:`Platform`.
    """

    flag: str | None
    env: str | None
    detected_model: str | None
    source: str
    platform: Platform


#: Where the Lyra reports itself in the device tree. P0 (2026-08-01) recorded a trailing
#: space before the NUL; re-verified live on-device during P1 (2026-08-01) as exactly
#: ``b"Luckfox Lyra\x00"`` — no trailing space. :func:`_read_device_tree_model` strips
#: whatever surrounds it either way, so this is a documentation correction, not a behaviour
#: change — flagged here in case a different unit/firmware revision does carry one.
_LYRA_MODEL = "Luckfox Lyra"

_DEVICE_TREE_MODEL_PATH = Path("/proc/device-tree/model")


def _read_device_tree_model() -> str | None:
    """Read and normalize ``/proc/device-tree/model``, or ``None`` when it doesn't exist.

    Absent on every machine that isn't running a device-tree-based Linux kernel — every
    desktop, every CI runner, and (today) every ``ssh`` session onto one of those. This is
    deliberately the *only* signal auto-detection consults (no terminal-size guessing), so
    a small window on the desktop is never mistaken for the device.
    """
    try:
        raw = _DEVICE_TREE_MODEL_PATH.read_bytes()
    except OSError:
        return None
    return raw.decode("utf-8", "replace").rstrip("\x00").strip()


def _lookup(name: str, *, source: str) -> Platform:
    """Resolve a platform name to its :class:`Platform`, or raise a clear ``ValueError``."""
    try:
        return _BY_NAME[name.strip().lower()]
    except KeyError:
        choices = ", ".join(sorted(_BY_NAME))
        # Plain ASCII: this can surface through Typer/Click's own error path (raised
        # before make_console() has reconfigured stdout/stderr to UTF-8), which mangles
        # non-ASCII on a legacy Windows console the way make_console's docstring warns of.
        raise ValueError(
            f"unknown platform {name!r} (from {source}) - choose from: {choices}"
        ) from None


def resolve(flag: str | None = None) -> Resolution:
    """Decide which platform is active, and record what the decision looked at.

    Resolution order: the explicit ``--platform`` flag, then the ``MESHTERM_PLATFORM``
    environment variable, then device-tree auto-detection, then :data:`REGULAR`. Only the
    input that actually decides the outcome is name-validated — an env var typo is
    reported when the flag isn't set, but never blocks a session that passed an explicit
    (correct) flag.

    Args:
        flag: The ``--platform`` command-line value, if given.

    Returns:
        The full :class:`Resolution`, for both immediate use (``.platform``) and the
        ``meshterm platform`` diagnostic's report.

    Raises:
        ValueError: The deciding input (flag or env var) named an unknown platform.
    """
    found = _resolve(flag)
    if _DRAWN_BY_MESHTERM and not found.platform.own_display:
        return replace(found, platform=replace(found.platform, own_display=True))
    return found


def _resolve(flag: str | None) -> Resolution:
    """:func:`resolve`'s decision, before :func:`drawn_by_meshterm` has its say."""
    env = os.environ.get("MESHTERM_PLATFORM") or None
    model = _read_device_tree_model()
    if flag:
        return Resolution(flag, env, model, "--platform flag", _lookup(flag, source="--platform"))
    if env:
        return Resolution(
            flag,
            env,
            model,
            "MESHTERM_PLATFORM env var",
            _lookup(env, source="MESHTERM_PLATFORM"),
        )
    if model == _LYRA_MODEL:
        return Resolution(flag, env, model, "auto-detect", PICOCALC_LYRA)
    return Resolution(flag, env, model, "default", REGULAR)


#: Set while :mod:`meshterm.emulator` runs MeshTerm in a display of its own drawing (see
#: :func:`drawn_by_meshterm`).
_DRAWN_BY_MESHTERM = False


@contextmanager
def drawn_by_meshterm() -> Iterator[None]:
    """Resolve every platform as one MeshTerm draws itself, for as long as this lasts.

    The emulator shows a PicoCalc (Lyra) in a window: the platform is that device's in every
    respect but one — no console draws it, MeshTerm does — so it resolves with
    :attr:`Platform.own_display` set, and nothing about the terminal the emulator was
    started from (a classic Windows console to leave, a font to offer) is the emulated
    session's business. In-process only: nothing leaks into a child process's environment.
    """
    global _DRAWN_BY_MESHTERM
    previous = _DRAWN_BY_MESHTERM
    _DRAWN_BY_MESHTERM = True
    try:
        yield
    finally:
        _DRAWN_BY_MESHTERM = previous


def without_emoji(platform: Platform) -> Platform:
    """The same platform with its icons routed through the compact glyph table.

    A terminal that cannot draw emoji is not a different *flavour* — the classic Windows
    console has the desktop's width, colour depth and keyboard, and wants every other
    thing :data:`REGULAR` says. Only the one flag moves, which is what the seam's flags
    being independent is for: ``font`` stays unset, so accents and the rest of the BMP
    still come through.

    Args:
        platform: The resolved platform.

    Returns:
        ``platform`` itself when its icons are already compact, else a copy with
        ``emoji`` cleared.
    """
    return platform if not platform.emoji else replace(platform, emoji=False)


__all__ = [
    "Platform",
    "REGULAR",
    "PICOCALC_LYRA",
    "CARDPUTER_ZERO",
    "without_emoji",
    "PLATFORM",
    "get_platform",
    "set_platform",
    "on_platform",
    "Resolution",
    "resolve",
]
