# SPDX-License-Identifier: Apache-2.0
"""The platform seam: a frozen spec that each consumer binds to once, at boot.

MeshTerm runs its flavours from one codebase: the regular desktop terminal, the
PicoCalc's 53-column framebuffer console, and the display of the Cardputer Zero (53×14
cells). There are no separate screens for each flavour and no runtime layer.
A :class:`Platform` is resolved one time, at the start of the process, and kept in a
module-level singleton. Each consumer downstream (the theme, the frame compositor, the
header, the width handling of the session) reads it when that consumer is constructed or
called, and binds its behaviour then. Thus the render loop itself never branches on the
platform.

Read the active platform through :func:`get_platform` (or the module-qualified
``platforms.PLATFORM``). Never use ``from meshterm.platforms import PLATFORM`` at the top
level of a module. That statement runs one time, at import time, and copies the value
that :data:`PLATFORM` had *then*. If :func:`set_platform` rebinds the module global
afterwards, the rebinding does not reach the old copy. A function call always reads the
current value, so it does not have this problem.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Platform:
    """The complete rendering and behaviour spec of one flavour.

    Each field is data that a consumer binds to when that consumer is constructed or called
    (refer to the module docstring). Only this module and the binding points that each
    field names branch on ``platform.name`` itself.

    Attributes:
        name: The stable identifier: ``"regular"``, ``"picocalc-lyra"``, or
            ``"cardputer-zero"``. MeshTerm uses it to match ``--platform`` and
            ``MESHTERM_PLATFORM``, and in diagnostics. The name of a handheld is its product
            and the variant that makes it different from its siblings. M5Stack makes three
            Cardputers, and only the Zero runs Linux. The PicoCalc takes several cores, and
            only the Luckfox Lyra runs MeshTerm.
        readable_cols: The readability standard (the rule in CLAUDE.md that screens stay
            readable at N columns) and the test width of the dual-platform gallery.
        readable_rows: The minimum number of rows that a screen is designed for. The live
            console of the PicoCalc is 53×26 (custom 6×12 font). A rebuild with a 6×8 font
            (P5) gives 53×40. Screens are designed for this minimum and use extra rows when
            they are there. The gallery renders picocalc-lyra at 26 rows and at 40 rows.
        frame_border: Whether the base screen draws inside a ``Panel`` with a border.
            ``False`` replaces the border with a title-bar row (4 more columns and 1 more
            row). This saves chrome on the PicoCalc. Wired in P4.
        menu_icons: Whether a command row starts with a decorative icon: the mark of each
            tool in the main menu, the ``🗑`` or ``🕒`` of an action list, the ``✎`` of an
            action in a body. The label already names the action, so the icon only shows
            the family of the action. It gives no information. Where the console can only
            spell the icon as a stand-in of one glyph, the icon is worth less than the cells
            that it uses, and it also looks like noise (``@`` for both Map and Mesh walk,
            ``…`` for both Live feed and Time machine, JP, on the handheld, 2026-08-11). ``False``
            removes the whole lane, the mark and the emoji. The cells go to the label and its
            description. This is only about decoration. A glyph that carries data is not a
            menu icon, and MeshTerm never removes it. Examples are the openness of a channel,
            the class of a packet, the type of a node, and a status mark on an outcome (refer
            to :func:`~meshterm.ui.menus.command_label`).
        dialog_margin: The total number of columns that a floating dialog leaves to the
            backdrop under it, half on each side (refer to
            :func:`~meshterm.ui.tui.frame._dialog_layout`). The gutter makes the dialog look
            like it floats, and not like a new screen. At 72 columns, three columns on each
            side are enough. On the console of 53 columns, the same gutter is one twelfth of
            the whole display, and it costs content. For example, the reception row of a
            packet card puts ``rssi`` on a line of its own, because two cells are missing
            (JP, on the handheld, 2026-08-10). Two columns on each side still look like floating,
            and they give the cells back.
        dialog_row_margin: The number of rows that a floating dialog leaves to the frame
            around it. These are the header above, the footer below, and each blank row
            between them and the box. One row of space above and one below make a box look
            like it floats on a terminal of 24 rows (the value is 4). The Cardputer has 14
            rows. There, each row that a dialog gives up is a row that it cannot show. The
            side gutters already show that the dialog floats. There is no header row above
            the title bar, so a tall box runs from the top row to the lane (the value is 1).
            It draws over the bar, and MeshTerm blanks the ends of the bar on each side of
            the box, so that no fragments remain (refer to
            :func:`~meshterm.ui.tui.frame.composite_float`). The box never draws over the
            lane. The lane of a dialog belongs to the dialog, and no other place names the
            keys on it.
        header_atoms: The segments that make the persistent header, from this vocabulary:
            ``"version"``, ``"device"``, ``"badges"``, ``"pulse"``, ``"battery"``. Wired into
            :func:`~meshterm.ui.menu._header` in P4. Nothing uses it before then.
        header_row: Whether the persistent header has a row of its own above the frame.
            If it does not (the Cardputer, JP 2026-10-03), the atoms of the header go at the
            right end of the title bar without a border, after the way out. Thus the badges
            and the battery stay in the top-right corner, one row up, and each screen gets
            that row back. The wordmark becomes the title of the main menu (refer to
            :func:`~meshterm.ui.menu._menu_title`). Only a frame without a border can fold the
            header in this way, because the panel with a border has no bar to carry it.
        lane_deck: The name of the F-key lane deck that the footer deals (refer to
            :data:`~meshterm.ui.tui.fkeys.DECKS`). It is ``""`` where the footer is the
            ``footer_hint`` string of each screen. A deck is all the parts of the lane that
            belong to one keyboard: the keycodes that drive its slots, the places of its
            chips on the row, how MeshTerm draws the chips, and which lane definitions of a
            screen it reads (``Screen.picocalc_lyra_lane``, ``Screen.cardputer_zero_lane``).
            Each handheld names its own deck. Thus a change to one deck does not change the
            other. :attr:`footer_fkeys` is the yes or no form of this field.
        width_reclaim: Whether the session can report one extra terminal column (refer to
            :class:`~meshterm.ui.tui.session._WidthExtendedOutput`). ``True`` only lets the
            session do it. The session reclaims the column where the size probe of the
            terminal hid one. This is the console output of prompt_toolkit on Windows, and
            nowhere else (:func:`~meshterm.ui.tui.session._probe_hides_last_column`).
            ``False`` on the console of the PicoCalc, which has the exact width, because a
            phantom extra column tears the frame. ``MESHTERM_FULL_WIDTH`` is still an
            explicit override of this default.
        emoji: Whether emoji icons render at all. ``False`` means that each icon funnel uses
            a compact table of single glyphs in the BMP instead (P3). Then neither part of the
            emoji alignment runs: the reserve-two measurement
            (:func:`~meshterm.ui.tui.emoji_width.install`) and the column pinning
            (:mod:`~meshterm.ui.tui.colsnap`). When MeshTerm never draws emoji, each glyph on
            the screen is a glyph that the console font was checked for, and the stock widths
            are exact.
        font: The name of the glyph inventory that the screen is drawn in (refer to
            :data:`~meshterm.ui.fontset.FONTS`). It is ``""`` where a terminal draws all that
            it receives. At the render boundary, MeshTerm folds each frame of a handheld down
            to its font (:func:`~meshterm.ui.theme.fold_text`). It removes an accent that the
            font does not have, changes an emoji to its compact glyph, and changes any other
            missing character to a narrow ``?``. Thus MeshTerm never sends the display a
            character that it cannot draw. The stored data does not change.
        truecolor: Whether the theme can use any 24-bit SGR colour. ``False`` selects a theme
            with a palette of 16 slots instead (the real limit of the console: no RGB for
            each cell). It also quantizes the two scales that would use a gradient: the hue
            of the key of each node (:func:`~meshterm.ui.theme.node_style`) and the heat of
            the heard age (:func:`~meshterm.ui.widgets._recency_style`). Wired in P3.
        solid_braille: Whether the console font draws the eight dots of a braille cell as
            solid tiles, with no gap between them or between neighbouring cells. The built
            fonts of the PicoCalc (``scripts/picocalc-lyra``) do this. Thus braille is a true
            2×4 pixel grid, fine enough for what must stay contiguous. Because of this, each
            QR code can be drawn in braille, with one dot for each module
            (:mod:`~meshterm.ui.qr`). The braille of a desktop font has gaps between the dots,
            and a code that is drawn in it does not scan.
        url_codes: Whether MeshTerm draws the URLs of a chat message as QR codes under the
            message, side by side (:meth:`~meshterm.ui.chat.ChatScreen._body_lines`). Thus the
            user can open a link that was read on the handheld on a phone, and does not type
            it. This is true only on the PicoCalc. It has braille codes
            (:attr:`solid_braille`), which have a quarter of the area of the half blocks that
            a desktop draws, and only then are they small enough to be in a transcript. It
            also has the rows. The code of a short link has 9 rows, and the 14 rows of the
            Cardputer cannot spare them under each message that has a link (JP, 2026-10-01).
        effects: Whether animated and decorative rendering runs at all. An example is the
            braille spinner, which changes to a ``LINE`` fallback. This costs less on a
            console where a paint is expensive. The battery gauge of the header is not
            controlled by this field. The sweep of the gauge is the charging state itself,
            and its blink is the last warning before the pack is empty. Both use the idle
            paint that happens in any case. Wired in P2/P3.
        tick_s: The refresh interval of the
            :class:`~prompt_toolkit.application.Application`. This is only the rate of the
            idle paint, because each screen pushes its own paint through
            ``TuiSession.invalidate`` when its data changes. MeshTerm composed one frame in
            74 ms on the PicoCalc at 53×26 (110 ms at 53×40). Thus an idle tick of 1 Hz alone
            costs 7–11% of a core that does nothing. On the PicoCalc, the tick has half that
            rate. The things that then change late are only cosmetic: the pulse of the header
            for each minute and its packet counter. Nobody can read them at a resolution of
            one second in any case.
        spinner_tick_s: The seconds between the animation steps of a "working" spinner. This
            is the only source for each animated wait in the app. This hardware cannot
            keep the 0.12 s of the desktop. A frame of the PicoCalc would use 62% of a core
            at 53×26. At 53×40, its 110 ms of composing would leave less than 10 ms of each
            interval for the work that the user waits for. Before the savings of this phase,
            the frame did not fit at all: it needed 152 ms against a budget of 120 ms. The
            spinner now has a rate that the hardware can keep, and it still looks alive.
        battery: The source of the battery gauge in the header. ``"companion"`` reads it from
            the connected MeshCore device. ``"host"`` reads it from the ``power_supply``
            driver of sysfs on the PicoCalc itself (confirmed present in P0). Wired in P5.
        own_display: Whether MeshTerm itself draws this display (:mod:`meshterm.emulator`)
            and does not run in a terminal. Then the app is its own terminal, and the
            terminal that started it is not its business. Examples are a classic Windows
            console to move out of and a font to offer. This is true on the Cardputer Zero,
            because its launcher gives an app no console. It is also true on each platform
            while the emulator shows the platform in a window.
        modifier_watch: The keyboard whose Shift state the optional evdev watcher follows.
            While Shift is held, the watcher changes the F-key lane on the screen at once.
            The value is a substring of the name of the input device, and case does not
            matter. ``""`` keeps the watcher off. This is for handhelds only. Desktop
            terminals have no lane to flip. The watcher needs access to ``/dev/input``, which
            the deploy user of a handheld has (the ``input`` group). It is always optional
            and lazy. Wired in P4.
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
        """Whether the footer is an F-key lane and not the hint string of each screen."""
        return bool(self.lane_deck)


#: Exactly the behaviour of the desktop or ssh terminal. The arrival of this seam did not
#: change it.
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

#: The framebuffer console of the PicoCalc, Lyra, and Calculinux. No binding point uses
#: some of the field values yet (header_atoms, font, truecolor, effects beyond the two P1
#: bindings, tick_s, battery, modifier_watch). They are the targets of P2 to P5. They are
#: recorded here now, so that the seam exists before the work on the flavour arrives.
PICOCALC_LYRA = Platform(
    name="picocalc-lyra",
    readable_cols=53,
    readable_rows=26,
    frame_border=False,
    menu_icons=False,
    dialog_margin=4,
    dialog_row_margin=4,
    # JP's decision (2026-08-01, after the P7 review): the header of the handheld shows the
    # name of the app ("MeshTerm vX") instead of the device or the port, because the port
    # of a soldered radio never changes. The activity pulse takes the room that remains.
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

#: The Cardputer Zero of M5Stack. Its display is 320×170 pixels, drawn in cells of 6×12
#: (53×14). Under it is a keyboard of 46 keys, and the number keys 4 to 8 are right below
#: the display. The hardware was not in hand on 2026-10-04. Thus only ``--platform
#: cardputer-zero`` or ``MESHTERM_PLATFORM`` chooses this platform. There is no
#: device-tree auto-detection until the device reports its own model string.
#:
#: It has the same 53 columns as the PicoCalc, so most of the flavour of the PicoCalc
#: applies. The new constraint is the number of rows. It has its own F-key lane deck (Fn+4…8,
#: with the chips centred over their keys). For now, the deck deals the lanes of the PicoCalc
#: (JP, 2026-09-30). MeshTerm draws the display itself in RGB565 (:mod:`meshterm.emulator`),
#: so the 16-slot palette does not limit it (``truecolor``). These values are provisional
#: until MeshTerm measures them on the device: the cadences, which come from the PicoCalc,
#: and ``effects`` off. The battery is the BQ27220 gauge of the handheld itself. The kernel
#: publishes it as the ``power_supply`` ``bq27220-0`` (seen on the device, 2026-10-05). It
#: is the pack that the user holds, with any connected radio, the same as on the PicoCalc.
CARDPUTER_ZERO = Platform(
    name="cardputer-zero",
    readable_cols=53,
    readable_rows=14,
    frame_border=False,
    menu_icons=False,
    dialog_margin=4,
    dialog_row_margin=1,
    # No header row (JP, 2026-10-03). The badges and the battery go at the right end of the
    # title bar, and the wordmark is the title of the main menu. The pulse is removed. It
    # only ever had the cells that the header did not use, and the dashboard draws the
    # activity of the mesh in full.
    header_atoms=("badges", "battery"),
    header_row=False,
    lane_deck="cardputer-zero",
    width_reclaim=False,
    emoji=False,
    font="cardputer-zero",
    truecolor=True,
    # The emulator draws its own braille, the same as the built fonts of the PicoCalc: solid
    # tiles, with no gap between the dots (meshterm/emulator/font.py, BRAILLE).
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

#: The active platform. Do not import this name directly (refer to the module docstring).
#: Read it through :func:`get_platform`, and change it only through :func:`set_platform`.
PLATFORM = REGULAR


def get_platform() -> Platform:
    """Return the active :class:`Platform`.

    You can call this function from anywhere, at any time. It always returns the platform of
    the most recent :func:`set_platform` call, in whatever way the module was imported.
    """
    return PLATFORM


#: Callbacks that run again at each :func:`set_platform`. Thus a module can bind state that
#: comes from the platform (a chosen theme, a changed function implementation) one time for
#: each switch, and does not derive it again at each call in a render loop. Register a
#: callback with :func:`on_platform`.
_BINDINGS: list[Callable[[Platform], None]] = []


def on_platform(binding: Callable[[Platform], None]) -> Callable[[Platform], None]:
    """Register a platform binding and run it at once.

    The principle of the seam is to bind at construction time, never for each frame. A
    consumer that must change its behaviour with the platform registers a binding here, at
    its own import time. Examples are the ``name_style`` implementation of the theme and the
    console cache of the rasterizer. The binding runs one time at once, with the platform
    that is active now. It runs again at each later :func:`set_platform`. Thus tests that
    change the platform bind again automatically, and the hot path of the consumer reads a
    plain module global.

    Args:
        binding: A callback that is called with the active :class:`Platform`. It must be
            idempotent.

    Returns:
        ``binding`` unchanged, so it can be used as a decorator.
    """
    _BINDINGS.append(binding)
    binding(PLATFORM)
    return binding


def set_platform(platform: Platform) -> None:
    """Install ``platform`` as the active one and re-run every registered binding.

    The CLI callback calls this function one time, before
    :func:`~meshterm.ui.theme.make_console`. Each other read of :func:`get_platform` in the
    same process happens after this call. The function is idempotent (if you set the same
    platform two times, the second call has no effect) and reversible. Thus a test can set a
    platform for one test and restore the previous platform afterwards (refer to the
    ``_reset_platform`` autouse fixture in ``tests/conftest.py``).

    Args:
        platform: The platform to make active.
    """
    global PLATFORM
    PLATFORM = platform
    for binding in _BINDINGS:
        binding(platform)


@dataclass(frozen=True, slots=True)
class Resolution:
    """The inputs that :func:`resolve` examined, and the platform that it chose.

    The ``meshterm platform`` diagnostic uses this class. Thus it can report each input that
    the resolution considered, and not only the final answer.

    Attributes:
        flag: The ``--platform`` value from the command line, if there is one.
        env: The value of the ``MESHTERM_PLATFORM`` environment variable, if it is set.
        detected_model: The contents of ``/proc/device-tree/model`` that the resolution read
            (without the NUL and the spaces at the end). It is ``None`` when the file does
            not exist (each machine that is not a Lyra, with each desktop and each CI
            runner).
        source: The input that decided the result: ``"--platform flag"``,
            ``"MESHTERM_PLATFORM env var"``, ``"auto-detect"``, or ``"default"``.
        platform: The resolved :class:`Platform`.
    """

    flag: str | None
    env: str | None
    detected_model: str | None
    source: str
    platform: Platform


#: Where the Lyra reports itself in the device tree. P0 (2026-08-01) recorded a space at the
#: end, before the NUL. During P1 (2026-08-01), a check on the handheld showed exactly
#: ``b"Luckfox Lyra\x00"``, with no space at the end. :func:`_read_device_tree_model` removes
#: all the characters around the name in both cases. Thus this is a correction of the
#: documentation and not a change of behaviour. It is noted here because a different unit
#: or firmware revision can have the space.
_LYRA_MODEL = "Luckfox Lyra"

_DEVICE_TREE_MODEL_PATH = Path("/proc/device-tree/model")


def _read_device_tree_model() -> str | None:
    """Read and normalize ``/proc/device-tree/model``, or return ``None`` if it does not exist.

    The file is absent on each machine that does not run a Linux kernel with a device tree:
    each desktop, each CI runner, and (today) each ``ssh`` session to one of these. This is
    the only signal that auto-detection uses, on purpose. It does not guess from the size of
    the terminal. Thus a small window on the desktop is never mistaken for the device.
    """
    try:
        raw = _DEVICE_TREE_MODEL_PATH.read_bytes()
    except OSError:
        return None
    return raw.decode("utf-8", "replace").rstrip("\x00").strip()


def _lookup(name: str, *, source: str) -> Platform:
    """Resolve a platform name to its :class:`Platform`, or raise a ``ValueError``."""
    try:
        return _BY_NAME[name.strip().lower()]
    except KeyError:
        choices = ", ".join(sorted(_BY_NAME))
        # Plain ASCII. This error can come through the error path of Typer and Click themselves.
        # They raise it before make_console() sets stdout and stderr to UTF-8. On a legacy
        # Windows console, this damages non-ASCII text, as the docstring of make_console
        # warns.
        raise ValueError(
            f"unknown platform {name!r} (from {source}) - choose from: {choices}"
        ) from None


def resolve(flag: str | None = None) -> Resolution:
    """Decide which platform is active, and record the inputs of the decision.

    The order of resolution is: the explicit ``--platform`` flag, then the
    ``MESHTERM_PLATFORM`` environment variable, then device-tree auto-detection, then
    :data:`REGULAR`. MeshTerm checks the name only for the input that decides the result.
    If the flag is not set, a typing error in the environment variable is reported. But
    the error never blocks a session that has an explicit and correct flag.

    Args:
        flag: The ``--platform`` value from the command line, if there is one.

    Returns:
        The full :class:`Resolution`. The caller can use it at once (``.platform``), and the
        report of the ``meshterm platform`` diagnostic uses it.

    Raises:
        ValueError: The input that decided the result (the flag or the environment
            variable) named an unknown platform.
    """
    found = _resolve(flag)
    if _DRAWN_BY_MESHTERM and not found.platform.own_display:
        return replace(found, platform=replace(found.platform, own_display=True))
    return found


def _resolve(flag: str | None) -> Resolution:
    """The decision of :func:`resolve`, before :func:`drawn_by_meshterm` has an effect."""
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


#: True while :mod:`meshterm.emulator` runs MeshTerm in a display that MeshTerm draws itself
#: (refer to :func:`drawn_by_meshterm`).
_DRAWN_BY_MESHTERM = False


@contextmanager
def drawn_by_meshterm() -> Iterator[None]:
    """Resolve each platform as a platform that MeshTerm draws itself, while the context lasts.

    The emulator shows a PicoCalc (Lyra) in a window. The platform is the platform of that
    device in each way but one: no console draws it, and MeshTerm does. Thus it resolves
    with :attr:`Platform.own_display` set. The terminal that started the emulator is not the
    business of the emulated session (for example, a classic Windows console to leave, or a
    font to offer). This works only in the process. Nothing goes into the environment of a
    child process.
    """
    global _DRAWN_BY_MESHTERM
    previous = _DRAWN_BY_MESHTERM
    _DRAWN_BY_MESHTERM = True
    try:
        yield
    finally:
        _DRAWN_BY_MESHTERM = previous


def without_emoji(platform: Platform) -> Platform:
    """The same platform, with its icons sent through the compact glyph table.

    A terminal that cannot draw emoji is not a different flavour. The classic Windows
    console has the width, the colour depth, and the keyboard of the desktop, and it needs
    all the other values that :data:`REGULAR` has. Only the one flag changes. This is the
    reason why the flags of the seam are independent. ``font`` stays unset, so accents and
    the rest of the BMP still go through.

    Args:
        platform: The resolved platform.

    Returns:
        ``platform`` itself if its icons are already compact. If not, a copy with
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
