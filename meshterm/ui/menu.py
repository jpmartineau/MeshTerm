# SPDX-License-Identifier: Apache-2.0
"""The interactive main menu, built dynamically from the tool registry.

The menu groups the registered tools by category. It runs the interactive flow of the
selected tool in the full-screen :class:`~meshterm.ui.tui.session.TuiSession`. The menu
comes from the registry, so when you add a tool, the menu automatically gets an entry for
it, with no changes here. The persistent header shows the connection target and the live
counters of the passive monitor. The prompts of each tool show as dialogs on top of each
other, and the output of the tool shows in a scrollable result window of limited size.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from rich.cells import cell_len
from rich.logging import RichHandler
from rich.markup import escape
from rich.text import Text

from .. import __version__
from ..context import AppContext
from ..persistence.logging import get_logger
from ..platforms import get_platform
from ..services import hold_to_quit
from ..tools import all_tools
from .braillechart import activity_peak, activity_sparkline
from .menus import (
    SEP_COMPACT,
    SEP_ROOMY,
    icon_lane,
    icon_mark,
    section_heading,
)
from .surface import TuiUi
from .theme import make_console
from .tui import (
    Choice,
    PopToMenu,
    ReconnectDialog,
    ScrollScreen,
    SelectScreen,
    Separator,
    TuiSession,
)
from .tui.emoji_width import install as install_emoji_widths
from .tui.fkeys import EMPTY_LANE, FPair
from .tui.spinner import spinner_interval
from .widgets import battery_cell

#: The grace period (seconds) for the full exit sequence, after the user decides to quit.
#: The sequence is the full-screen unwind, the device disconnect, and the atexit thread
#: joins of the interpreter. The period is much longer than a usual teardown
#: (approximately 1 s), so the watchdog fires only when the exit is really stuck.
_EXIT_WATCHDOG_S = 5.0

#: How often (seconds) the liveness watcher checks that the serial port of the connected
#: companion is still present, while the menu is idle. The ``meshcore`` client gives
#: cached data and never raises an error when the cable is disconnected. Thus this poll of
#: the port in the OS, and not a failed command, is what finds a disconnected cable
#: (refer to :func:`~meshterm.core.connection.serial_port_present`).
_LIVENESS_POLL_S = 2.0

#: Debounce (seconds): when the port first seems to be gone, wait this long and check
#: again before MeshTerm says that the link is lost. Thus a short gap in the enumeration
#: (driver changes while the cable is connected again) cannot cause a false
#: "disconnected" prompt.
_LIVENESS_CONFIRM_S = 0.4

#: How long (seconds) each round of the reconnect flow listens for the BLE advertisement
#: of a Bluetooth companion that disconnected. The scan stops immediately when it hears
#: the address. Thus this value is a limit for a miss, not a cost for a success. It is
#: long enough for several advertising intervals of a companion that has just started.
_BLE_PRESENCE_SCAN_S = 8.0

#: The pause (seconds) between BLE presence scans. The scan window is the real wait. This
#: pause only stops a fast loop when the scan fails immediately (no adapter, or Bluetooth
#: is off).
_BLE_RESCAN_PAUSE_S = 1.0

#: After a reboot that we sent, how long (seconds) MeshTerm waits before the first try to
#: connect again. The dialog opens immediately after the command goes out, and at that
#: time the board possibly did not start its restart. Without this wait, a port that
#: stays through the reboot is opened again against the old session, and that session
#: stops a moment later.
_REBOOT_SETTLE_S = 1.5

#: How many tries to connect again must fail in sequence before the dialog shows the
#: reason. At that time the device is present (each try waits for its port or its BLE
#: advertisement), but a board can refuse one time while it boots, and that is not
#: important enough to show.
_REASON_AFTER_FAILURES = 2


def _arm_exit_watchdog(seconds: float = _EXIT_WATCHDOG_S) -> None:
    """Make sure that the process exits, also if the exit path gets stuck.

    MeshTerm calls this function one time, at the start of the teardown, after it released
    the async resources of the app. If a clean exit completes within ``seconds``, the
    process is already gone and this daemon thread stops with it. Thus the usual path does
    not change. The watchdog acts only when the exit hangs. Most often, the cause is the
    Windows input reader of prompt_toolkit, which is a non-daemon executor thread. In rare
    races during the teardown, this thread stays blocked in a Win32 wait, and it stops the
    thread joins of the interpreter at exit. (The watchdog is also a backup for a stall in
    the device disconnect.) A daemon thread can still run while the main thread is blocked
    in that join, so the watchdog can force the process to exit.

    The committed database writes are durable in all cases (MeshTerm commits each write
    when it occurs). Thus a hard exit here loses no data.

    Args:
        seconds: The grace period before the forced exit.
    """

    def _bail() -> None:
        time.sleep(seconds)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os._exit(0)

    threading.Thread(target=_bail, name="meshterm-exit-watchdog", daemon=True).start()


@contextmanager
def _silence_console_logging() -> Iterator[None]:
    """Detach the console log handlers while the full-screen TUI runs.

    The full-screen session owns the terminal through prompt_toolkit. A handler that
    writes log lines to the same console (the :class:`RichHandler` that
    :func:`configure_logging` installs) corrupts the frame. This is most visible when the
    monitor connects the device at startup and logs INFO records. The file logging does
    not change, so the JSON-lines log stays complete. The handlers are attached again at
    exit.
    """
    logger = get_logger()
    detached = [h for h in logger.handlers if isinstance(h, RichHandler)]
    for handler in detached:
        logger.removeHandler(handler)
    try:
        yield
    finally:
        for handler in detached:
            logger.addHandler(handler)


#: The minimum number of sparkline cells that the header tries to keep. If the fixed
#: segments with the wide "  ·  " separators leave fewer cells, the header lays itself out
#: again with the compact " · ". Thus a 72-cell terminal still shows a part of the pulse
#: that is long enough to read.
_SPARK_MIN_CELLS = 24

#: The floor for the scale ceiling of the header pulse (refer to
#: :func:`~meshterm.ui.braillechart.activity_peak`). A single packet in a time window that
#: was silent for a long time draws against a minimum of this many packets per minute.
#: Thus one packet shows as a small bump, not as a full column.
_HEADER_ACTIVITY_FLOOR = 3.0


def _header(ctx: AppContext, cache: dict, width: int) -> Text:
    """Build the persistent one-line header: the connected node, unread messages, the pulse.

    From left to right, the header shows:

    - the app mark,
    - the name of the connected node, with the connection to it (``(COM5)`` / ``(BLE)``),
    - the badges for unread messages and for the Watchtower (only when something waits),
      and
    - the braille activity pulse. The pulse counts each packet that the hub hears, at one
      minute for each dot column, with the newest at the right edge, as in each MeshTerm
      timeline.

    The pulse fills the middle of the row. A battery gauge (only when the companion
    reports a battery pack) is pinned to the right edge, and the pulse gives it the cells
    that are necessary for it. Thus a wider terminal shows more history. The separators are wide by
    default. They change to a compact ``·`` when a wide separator makes the pulse shorter
    than :data:`_SPARK_MIN_CELLS`.

    Args:
        ctx: The shared application context, read live at each paint.
        cache: Scratch space that the session owns (refer to :func:`_device_label`).
            With it, the device-name lookup does not read the registry file again at each
            paint.
        width: The terminal width in cells. The sparkline uses all the cells that the
            fixed segments leave.

    Where the platform has no header row (:attr:`~meshterm.platforms.Platform.header_row`,
    the Cardputer), the same atoms go at the right end of the title bar instead. Then this
    function returns only these atoms: the badges, then the battery, with one space
    between them, with no padding and no pulse. The title bar places them, and the
    wordmark is the title of the main menu there (:func:`_menu_title`).

    Returns:
        A Rich :class:`Text` that shows at the top of each screen. The frame crops it to
        one line (refer to ``frame.compose_base``). Thus a terminal that is too narrow
        cuts the end, and the header does not wrap.
    """
    # The platform data sets which items the header has (refer to Platform.header_atoms).
    # On the 53 cells of the PicoCalc, the header removes the version mark and the pulse,
    # and keeps the device, the badges, and the battery. Read it live: set_platform runs
    # before the session, but tests change the platform.
    platform = get_platform()
    atoms = platform.header_atoms
    # The battery gauge is pinned to the right edge of the row. Thus reserve its width
    # (plus a separator before it) before the pulse takes the remaining cells. If the
    # estimate of the separator for the fit decision is too wide, the extra space does no
    # harm.
    battery = _battery_segment(ctx) if "battery" in atoms else Text()
    if not platform.header_row:
        return Text(" ").join(seg for seg in (*_header_segments(ctx, cache), battery) if seg)
    # The choice of separator changes only the joins, never the segments. Thus the
    # segments are built one time, and each possible width is arithmetic: one separator
    # for each segment (each separator comes after one segment, and the last separator
    # leads into the pulse). Before, the code built the segments two times to measure the
    # second option. That was a waste on the narrow terminals that use the compact branch
    # at each paint, which include the 53 cells of the PicoCalc.
    pieces = _header_segments(ctx, cache)
    content = sum(piece.cell_len for piece in pieces)

    def _measure(sep: str) -> tuple[int, int]:
        """The width of the joined header and the right-edge reserve, for one separator."""
        joined = content + len(pieces) * cell_len(sep)
        return joined, (cell_len(sep) + battery.cell_len) if battery.cell_len else 0

    sep = SEP_ROOMY
    header_w, reserve = _measure(sep)
    if width - header_w - reserve < _SPARK_MIN_CELLS:
        sep = SEP_COMPACT
        header_w, reserve = _measure(sep)
    header = Text()
    for piece in pieces:
        header.append_text(piece)
        header.append(sep)
    # Two dot columns for each cell: each cell left of the reserved end shows two minutes.
    room = width - header_w - reserve
    if "pulse" not in atoms:
        room = 0
    if room > 0:
        # The buckets that come from the stored history of a previous session draw in
        # grey. Only the traffic that this session heard shows in green.
        styles = ["ok" if live else "muted" for live in ctx.monitor.activity_session_flags()]
        # Scale to a steady ceiling over the full six-hour history of the monitor, which
        # is longer than the row draws. Do not use only the maximum of the drawn time
        # window. Use a peak that ignores outliers and has a floor (refer to
        # activity_peak). Thus the pulse does not jump when a busy minute scrolls off the
        # edge, and a single packet in a quiet period stays a small bump.
        histogram = ctx.monitor.activity_histogram()
        header.append_text(
            activity_sparkline(
                histogram,
                room * 2,
                peak=activity_peak(histogram, floor=_HEADER_ACTIVITY_FLOOR),
                column_styles=styles,
            )
        )
    if battery.cell_len:
        # Pin the gauge to the right edge. After the pulse fills `room`, the separator and
        # the gauge go against the edge. If there is no space for a pulse (a very narrow
        # terminal), pad instead, so that the gauge is still in the corner and not
        # directly after the text.
        if room <= 0:
            header.append(" " * max(0, width - header_w - reserve))
        header.append(sep)
        header.append_text(battery)
    return header


#: The minimum seconds for each animation step of the battery gauge in the header (the
#: charging sweep and the low-battery blink). The step is the idle paint interval of the
#: platform (``Platform.tick_s``, the ``refresh_interval`` of the app). Thus each paint
#: moves the animation by exactly one step, and no step is lost between two paints. The
#: step is 1 s on the desktop, and 2 s on the slower tick of the PicoCalc. Thus, on the
#: PicoCalc, a full sweep takes :data:`~meshterm.ui.widgets._CHARGE_FRAMES` × 2 s.
_BATTERY_ANIM_MIN_S = 1.0


def _battery_segment(ctx: AppContext) -> Text:
    """The header's battery gauge at the right end, or an empty Text when there is no pack.

    This function reads the cached snapshot of the poller
    (:meth:`~meshterm.services.battery_service.BatteryService.reading`), never the
    device. Thus it is safe on the render path. The animation step comes from the wall
    clock, so the charging sweep and the low-battery blink move by themselves, and the
    header does not have to pass a counter.

    Args:
        ctx: The shared application context, read for the latest battery snapshot.

    Returns:
        The gauge as a Rich :class:`Text` (glyph + ``%``), or an empty Text when the
        companion reports no battery.
    """
    reading = ctx.battery.reading()
    if reading is None:
        return Text()
    # The two animations of the gauge run on each platform. Only the step gets longer, to
    # the paint interval of that platform. Neither animation is behind `Platform.effects`.
    # The header is built again at each idle tick in all cases, so an animation costs only
    # a colour change in a frame that MeshTerm paints already. Also, the two animations
    # show "power is coming in" and "this handheld is about to die", and neither of these
    # is a decoration.
    frame = int(time.monotonic() / max(_BATTERY_ANIM_MIN_S, get_platform().tick_s))
    return battery_cell(reading.percent, charging=reading.charging, frame=frame)


def _wordmark() -> Text:
    """The app's mark as the header draws it: ``MeshTerm`` in brand, and its version muted."""
    # Built with one append for each part, not under a base style. A base style puts the
    # brand colour of the mark on the version too.
    mark = Text()
    mark.append("MeshTerm", style="brand")
    mark.append(f" v{__version__}", style="muted")
    return mark


def _header_segments(ctx: AppContext, cache: dict) -> list[Text]:
    """The fixed segments of the header (all that is left of the pulse), not joined.

    The segments have no separators by design. The separator that fits depends on the
    width of the segments, and the choice does not change the segments. Thus
    :func:`_header` measures the two options with arithmetic on this one build, and joins
    one time.

    Args:
        ctx: The shared application context.
        cache: The device-label cache (refer to :func:`_device_label`).

    Returns:
        The segments in the order on the screen: the app mark, the device, then the
        badges that have something to report. :func:`_header` writes one separator after
        each segment.
    """
    atoms = get_platform().header_atoms
    segments: list[Text] = []
    # Each segment starts as a Text with no style, and gets its styles with each append.
    # If the constructor gets a base style, that style goes under all the text appended
    # after it, and the brand style of the mark goes under the muted style of the version.
    if "version" in atoms:
        segments.append(_wordmark())

    if "device" in atoms:
        device = Text()
        if ctx.mock:
            device.append("simulator", style="warn")
        else:
            name, where = _device_label(ctx, cache)
            device.append(name or "no device", style=None if name else "muted")
            if where:
                device.append(f" ({where})", style="muted")
        segments.append(device)

    if "badges" in atoms:
        unread = ctx.chat.unread_total()
        if unread:
            badge = Text()
            badge.append("●", style="err")
            badge.append(f" {unread}", style="warn")
            segments.append(badge)
        alerts = ctx.watchtower.unacked_count()
        if alerts:
            # The badge of the Watchtower: a triangle, so that it never looks like unread
            # messages.
            badge = Text()
            badge.append("▲", style="err")
            badge.append(f" {alerts}", style="warn")
            segments.append(badge)
    return segments


def _device_label(ctx: AppContext, cache: dict) -> tuple[str, str]:
    """The device segment of the header: ``(node name, where)``, cached for each connection.

    For the name, the first choice is the mesh node name of the device itself (which the
    device registry stores at connect time), then the profile alias, then the discovered
    hardware name. This is the same order that the NAME column of the startup picker
    uses. ``where`` is the serial port, ``"BLE"`` for a Bluetooth companion, the endpoint
    (or ``"TCP"``) for a TCP connection, or ``"SPI"`` for an SPI connection. The registry
    is in a file, so the lookup is cached under the identity of the connection. MeshTerm
    reads the file again only when that identity changes.

    Args:
        ctx: The shared application context.
        cache: A dict that the caller owns. It holds one ``(key, value)`` pair.

    Returns:
        The ``(name, where)`` pair. Each of the two can be empty when it is not known.
    """
    sel = ctx.selected_device
    key = (
        sel.stable_id if sel is not None else None,
        ctx.profile_name,
        ctx.active_transport,
        ctx.active_port,
        ctx.active_address,
        ctx.active_endpoint,
    )
    if cache.get("key") == key:
        return cache["value"]

    from .device_picker import _hardware_name

    record = (
        ctx.device_store.load_all().get(sel.stable_id)
        if sel is not None
        else ctx.device_store.load()  # a stored reconnect: nothing discovered in this session
    )
    name = (record.node_name if record is not None else "") or ctx.profile_name or ""
    if not name and sel is not None:
        name = _hardware_name(sel)
    if not name and record is not None:
        name = record.label
    if ctx.active_transport == "ble":
        where = "BLE"
    elif ctx.active_transport == "tcp":
        where = ctx.active_endpoint or "TCP"
    elif ctx.active_transport == "spi":
        where = "SPI"
    else:
        where = ctx.active_port or (sel.port if sel is not None else "") or ""
    cache["key"], cache["value"] = key, (name, where)
    return name, where


async def run_menu(ctx: AppContext) -> None:
    """Run the interactive menu loop in the full-screen session until the user quits.

    This function installs the TUI surface and starts the session. Then it runs the device
    selection, the resume of the monitor, and the menu loop on the event loop of the
    session. The full-screen frame closes before the closing message prints.

    Args:
        ctx: The shared application context.
    """
    # Reserve two cells for each glyph that can draw as an emoji, in each width authority,
    # before prompt_toolkit lays out its first frame. The session pins each of these glyphs
    # into its reservation, so the rows stay aligned whatever the terminal font does with
    # the glyph. Skip this step on a platform that never draws emoji (the PicoCalc). There,
    # its own font verified each glyph, and the standard widths are already exact.
    if get_platform().emoji:
        install_emoji_widths()

    header_cache: dict = {}
    session = TuiSession(header=lambda cols: _header(ctx, header_cache, cols))
    ctx.ui = TuiUi(session)
    # The one quit confirm. The Quit row of the menu asks it, and so does the quit chord
    # from any screen.
    session.set_quit_confirm(lambda: _confirm_quit(ctx, session))
    # What a held Esc engages while its dialog is open. The app keeps it while it exits.
    session.set_quiesce(lambda: _off_the_air(ctx))
    # Where a terminal gives the keys, MeshTerm asks the keyboard whether Esc is still
    # held. Thus a held Esc quits there too. The emulator reads its keys itself, and the
    # keyboard of its machine is not the keyboard of the handheld.
    if not get_platform().own_display:
        session.set_esc_probe(hold_to_quit.esc_probe())

    async def main() -> None:
        try:
            if await _startup(ctx):
                await _session_loop(ctx, session)
        finally:
            # Stop the history and chat recording (this closes their run records), the
            # background advert scheduler, and the event hub that is always on, also on
            # an unexpected exit.
            await ctx.adverts.aclose()
            await ctx.clock_sync.aclose()
            await ctx.monitor.aclose()
            await ctx.chat.aclose()
            await ctx.events.aclose()
            # If the user selected "Unpair & quit", remove the OS bond now: after the
            # services stop and the link closes, because a bond cannot be removed cleanly
            # while it is in use. This is best-effort: a failure must not block the exit.
            if ctx.unpair_on_exit:
                await _unpair_on_exit(ctx)
            # All that the app owns is released. The remaining exit steps (the full-screen
            # unwind of prompt_toolkit, the device disconnect in the CLI driver, and the
            # atexit thread joins of the interpreter) must never hang the process. Refer
            # to _arm_exit_watchdog.
            _arm_exit_watchdog()

    with _silence_console_logging():
        await session.run(main())
    ctx.console.print("[muted]bye 73![/muted]")


@asynccontextmanager
async def _off_the_air(ctx: AppContext) -> AsyncIterator[None]:
    """Hold the transmit lock of the connected device, so that no new transmission starts.

    This is the quiesce of the app (:meth:`~meshterm.ui.tui.session.TuiSession.set_quiesce`).
    MeshTerm holds it while the dialog of a held Esc is open, and keeps it while the app
    exits. Each transmission takes the lock (:mod:`~meshterm.core.transmit_lock`). Thus,
    when this function takes the lock, it first waits for a transmission that is in
    progress (a scoped channel send first closes its window and sets the session scope
    back). Then it stops the other transmissions: a courier retry, or a scheduled advert.
    When the user releases Esc, the lock is released, and these transmissions continue.
    With no device connected, there is nothing to hold, and this function never connects
    a device only to hold its lock.

    Args:
        ctx: The shared application context.
    """
    lock = getattr(ctx._device, "transmit_lock", None)
    if lock is None:
        yield
        return
    async with lock.held():
        yield


async def _can_unpair(ctx: AppContext) -> bool:
    """Whether the quit dialog offers to remove the OS pairing of the current device.

    This is true only when this session is on a Bluetooth link to a peripheral for which
    Windows has a bond. Thus the button shows for a companion that is paired with a PIN.
    It never shows for a serial port, an open BLE companion (with no PIN), or a platform
    on which we cannot unpair. The bond query shows the OS pairing (independently of the
    stored record of MeshTerm). Thus the button shows correctly also for a device that was
    bonded in an earlier session and connected again here without a PIN.

    Args:
        ctx: The shared application context.

    Returns:
        ``True`` if the dialog must show an unpair-and-quit button.
    """
    if ctx.active_transport != "ble":
        return False
    address = ctx.active_address
    if not address:
        return False
    from ..core.connection import MeshCoreDevice

    return await MeshCoreDevice.is_ble_paired(address)


async def _unpair_on_exit(ctx: AppContext) -> None:
    """Close the live connection and remove the OS bond of the device, while the app exits.

    The order is intentional. First, the companion link disconnects (a bond cannot be
    removed cleanly while an open connection uses it). Then Windows forgets the pairing,
    so that the next start of MeshTerm does the PIN procedure again. The stored device
    record of MeshTerm does not change: the unpair forgets the credential, not the
    identity of the device. All steps are best-effort: the function ignores each failure,
    so that a failure can never make the exit stuck.

    Args:
        ctx: The shared application context (as a side effect, its device disconnects).
    """
    address = ctx.active_address
    if ctx._device is not None:
        try:
            await ctx._device.disconnect()
        except Exception:  # noqa: BLE001 - a dead or lost link must not block unpair or exit
            pass
        ctx._device = None
    if address:
        from ..core.connection import MeshCoreDevice

        await MeshCoreDevice.unpair_ble(address)


#: The icon at the start of the last row of the menu, the Quit row. It has a name, and is
#: not written inline in the row, because the icon column of the menu must measure it: it
#: is one of the icons that the list can show, the same as the icon of each tool.
_QUIT_ICON = "🚪"

#: The value with which the Quit row resolves. This value opens the quit confirm (refer to
#: :func:`_menu_round`). Esc at the menu does not open it: Esc does nothing there (refer to
#: :class:`_MainMenu`).
_QUIT_VALUE = "__quit__"


async def _confirm_quit(ctx: AppContext, session: TuiSession) -> bool:
    """Open the quit confirm as a dialog over the current screen, and return whether to leave.

    There is one dialog for the two ways in: the Quit row of the menu, and the quit chord,
    which asks from any screen (refer to
    :meth:`~meshterm.ui.tui.session.TuiSession.request_quit`). Cancel (Esc) is to the left
    of Quit (Enter). Quit is highlighted at the start, so Enter commits it.

    The dialog offers to remove the OS pairing while the app exits, but only when there is
    a live Bluetooth bond to remove. It never offers this on serial, for an open companion
    (with no PIN), or on a platform on which we cannot unpair. That button is between
    Cancel and Quit and is never the default. Thus the user must select it intentionally,
    and an accidental Enter does not select it. The unpair itself occurs later, in the
    teardown (refer to :func:`run_menu`), because the link must disconnect first. The
    stored device record is kept intentionally, so the device only asks for its PIN again.

    Args:
        ctx: The shared application context (``unpair_on_exit`` is set on "Unpair & quit").
        session: The running TUI session.

    Returns:
        ``True`` if the user selected Quit or "Unpair & quit".
    """
    buttons = [("Cancel", "cancel")]
    if await _can_unpair(ctx):
        buttons.append(("Unpair & quit", "unpair"))
    buttons.append(("Quit", "quit"))
    # On a handheld, the question keeps the chip that asked it. F3, where the ``Quit?`` of
    # the menu was, shows ``Quit!`` here and sends the same ``quit``. While this confirm is
    # open, that action leaves immediately (refer to TuiSession.request_quit), as a second
    # ^Q does. Thus the way out from the menu is one key pressed two times (JP,
    # 2026-10-02).
    lane = list(EMPTY_LANE)
    lane[2] = FPair("Quit!", "quit")
    choice = await session.button_dialog(
        "Are you sure you want to quit?",
        buttons,
        title="Quit MeshTerm",
        default=len(buttons) - 1,  # highlight Quit
        # The hint stays the default hint of the dialog. Enter commits the highlighted
        # button, which is Quit only until ←→ moves the highlight. With a third button in
        # the row, the hint must name the keys that move the highlight.
        prompt_style="warn",
        button_style="selected",
        button_idle_style="muted",
        border_style="warn",
        lane=lane,
    )
    if choice == "unpair":
        ctx.unpair_on_exit = True
    return choice in ("unpair", "quit")


#: The heading of the main menu, where a header row has the wordmark above it.
_MENU_PROMPT = "What would you like to do?"


def _menu_title() -> str:
    """The heading of the main menu: the question, or the wordmark where no header row has it.

    On a platform without a header row (the Cardputer), the badges and the battery of the
    header go in the title bar, and the wordmark has no other place to go. Thus the root
    screen (the screen on which the app opens, and to which each ^W goes back) shows the
    wordmark as its title instead (JP, 2026-10-03). Each other screen names what it is.
    The question of the menu tells nothing that the user does not already know, and it is
    the one heading that we can replace.
    """
    return _MENU_PROMPT if get_platform().header_row else _wordmark().plain


class _MainMenu(SelectScreen):
    """The list of the main menu. On it, Esc clears a typed filter, and otherwise does nothing.

    A user who presses Esc many times goes back to the menu, which is the usual way. When
    Esc at the menu opened the quit confirm, the key presses after the user got to the
    menu opened the confirm too: the user asked for the menu and got a question about how
    to leave (issue #22). The menu is the bottom of the stack, so Esc has no screen to go
    back to. To leave, the user selects the Quit row or uses the quit chord, and both ask
    first.

    Thus the footer ends on the quit chord, not on Esc. While a filter is present,
    ``Esc clear`` comes last, because that is what the key press does then. It is the
    same atom that each list with a filter shows.
    """

    def handle(self, action: str, data: str = "") -> None:
        """Answer each key as a select list does, but a bare Esc does nothing."""
        if action == "escape" and not self._filter:
            return
        super().handle(action, data)

    def bar_title(self, title: str, style: str) -> Text:
        """The wordmark keeps the header's colours where it is the title (:func:`_menu_title`)."""
        mark = _wordmark()
        return mark if title == mark.plain else super().bar_title(title, style)

    @property
    def picocalc_lyra_lane(self):
        """The lane of the list, with the way out on F3: ``Quit?`` asks, ``Quit!`` does not.

        The PicoCalc has no hint line, so the lane must show the way out of the menu. F3 is
        the one free slot of the list (F1/F2 are the section jumps, and a menu row can
        never be deleted). ``Quit?`` opens the same confirm as the Quit row and ^Q. Its
        Shift half, F8, leaves without a question. This two-key press is only on the menu,
        for the user who has already decided (JP, 2026-09-29).
        """
        lane = list(super().picocalc_lyra_lane)
        lane[2] = FPair("Quit?", "quit", "Quit!", "quit_now")
        return lane

    @property
    def footer_hint(self) -> str:
        """The hint of the list. It gets ``Esc clear`` only while a filter is present."""
        base = super().footer_hint
        return f"{base} · Esc clear" if self._filter else base


def _menu_lane(tools: Sequence[Any]) -> int:
    """The icon column of the main menu in cells: the icons of all tools and of the Quit row.

    The Quit row is in the same list as the tool rows, so it must use their column too.
    Before, the row wrote ``🚪 Quit`` as a plain string outside the measurement. That row
    was aligned only because ``🚪`` had the same width as the widest tool icon. With a
    one-cell quit icon, "Quit" starts one column too early. With a three-cell tool icon
    (for example, a ZWJ cluster), "Quit" is one column too late. When the
    column is measured over all these icons, the alignment is correct by design, not by
    chance.

    Args:
        tools: The visible tools of the menu.

    Returns:
        The column width from :func:`~meshterm.ui.menus.icon_lane`: ``0`` where the
        platform draws no icon lane.
    """
    return icon_lane([*(tool.icon for tool in tools), _QUIT_ICON])


def _menu_labels(tools: Sequence[Any], *, lane: int | None = None) -> list[Text]:
    """The menu label of each visible tool: the icon, its column, and the title after it.

    The icons of the menu do **not all have one width**: ``⚙`` draws one cell, and ``📡``
    draws two. The terminal decides which icon has which width. Thus a row that wrote
    ``icon + " "`` started its title one column to the left of the rows with two-cell
    icons. That is why "Preferences" was not aligned with all of the menu above it (JP,
    2026-09-06). The column is measured one time over each icon that the menu can show,
    and each icon is padded to it, as the action list of a screen does
    (:func:`~meshterm.ui.menus.icon_lane`). Where the platform draws no icon lane, the
    column measures zero, and the titles use all the cells, flush.

    Args:
        tools: The visible tools of the menu, in the order in which they are drawn.
        lane: The width of the icon column, when the caller already measured it for all
            of the list (refer to :func:`_menu_items`). ``None`` measures it here, over the
            same icons (the icons of the tools and of the Quit row). Thus, in the two
            cases, the answer is the one that :func:`_menu_lane` gives.

    Returns:
        One label for each tool, in the same order. Each label is a new
        :class:`~rich.text.Text`, and the caller can append its description to it.
    """
    width = _menu_lane(tools) if lane is None else lane
    labels: list[Text] = []
    for tool in tools:
        label = icon_mark(tool.icon, "", width)
        label.append(tool.title or tool.name)
        labels.append(label)
    return labels


def _menu_items(tools: Sequence[Any]) -> list:
    """The rows of the main menu: the tools under their section headings, then Quit.

    This function is separate from :func:`_menu_loop`, so that the list can be built and
    checked without a session. It is a pure function of the tools. That is also why the
    loop builds the list one time and keeps it for all of the session.

    The rows have two columns, the name of the tool and then its muted description, and
    no header line. They are commands, not table data, so the alignment is sufficient.
    The Quit row gets its icon from the **same measured icon column** as the tool rows
    (refer to :func:`_menu_lane`), so its word starts in the same cell as each title
    above it. The platform sets the width of the column. Where the platform draws no icon
    lane, :func:`~meshterm.ui.menus.icon_mark` returns nothing, the separator included.
    Then the row is the bare word "Quit", flush with the titles, which are also flush.

    Args:
        tools: The visible tools of the menu, in the order in which they are drawn
            (already sorted by category, so that each heading opens its section only one
            time).

    Returns:
        The :class:`~meshterm.ui.tui.Choice` and :class:`~meshterm.ui.tui.Separator`
        rows, ready for the :class:`~meshterm.ui.tui.SelectScreen` of the menu.
    """
    lane = _menu_lane(tools)
    labels = _menu_labels(tools, lane=lane)
    name_w = max((label.cell_len for label in labels), default=0)
    items: list = []
    current_category: str | None = None
    for tool, row in zip(tools, labels, strict=True):
        if tool.category != current_category:
            current_category = tool.category
            items.append(section_heading(current_category))
        row.append(" " * (name_w - row.cell_len + 2))
        row.append(tool.help, style="muted")
        # The name of the tool is the identity of the row, and it always fits. Only the
        # description can be too long, so ←→ move only the description, and the name
        # stays (Choice.hscroll_from).
        items.append(Choice(title=row, value=tool.name, hscroll_from=name_w + 2))
    items.append(Separator(" "))
    # Quit has no description, so it is not part of name_w. Only its icon uses the icon
    # column of the tools.
    quit_label = icon_mark(_QUIT_ICON, "", lane)
    quit_label.append("Quit")
    items.append(Choice(title=quit_label, value=_QUIT_VALUE))
    return items


async def _menu_loop(ctx: AppContext, session: TuiSession) -> None:
    """Show the tool menu and run the selections until the user quits.

    The menu is the navigation **root** of the app. MeshTerm builds it one time and keeps
    it for all of the session. Thus the highlight, a typed filter, and the scroll offset
    are still there when a tool closes. No ``default=`` restore is necessary, and such a
    restore could only recover the highlight. The menu is also where the pop-all key goes:
    ^W raises :class:`~meshterm.ui.tui.screen.PopToMenu` through each stack frame that the
    user opened, and this is the one place that catches it.

    The menu is still popped while a full-screen tool runs (only a ``popup`` tool shows
    over it). That is presentation, not navigation, because the tool hides the menu in
    all cases. The screen object stays after the pop, and that is why a return feels like
    a pop and not like a rebuild. The screen object is also what the lead-in question of
    a tool shows over. The menu is declared as the root of the session, so it is the base
    that is pushed under a dialog that is otherwise the only stack frame
    (``TuiSession._floated``). Thus a dialog always shows around it the page from which
    the user opened it. For the same reason, the root stays declared while the loop
    exits, because the reconnect dialog draws over it. The next menu declares itself.

    Args:
        ctx: The shared application context.
        session: The running TUI session.
    """
    tools = [tool for tool in all_tools() if tool.menu_visible]
    items = _menu_items(tools)

    # Drive the menu list here (instead of through session.select), so that it stays on
    # the stack while the quit dialog shows over it. The confirm is drawn as a centred box
    # on top of the menu, which is still visible, not as a screen that replaces the menu.
    menu = _MainMenu(
        _menu_title(),
        items,
        # The one place that names the quit chord. The menu is where a user looks for the
        # way out, and Esc does nothing here, so without this atom the footer names no way
        # out. On all other screens, the hint does not show the chord, the same as ^W
        # (refer to session._CTRL_LETTER_CHORDS).
        footer_hint="↑↓ move · type to filter · Enter select · ^Q quit?",
    )
    session.set_root(menu)
    loop = asyncio.get_running_loop()
    while True:
        try:
            await _menu_round(ctx, session, menu, tools, loop)
        except _Quit:
            return
        except PopToMenu:
            # ^W from a deep screen. Each stack frame between there and here has already
            # popped itself. The only remaining step is to disarm and paint the menu that
            # the user asked for.
            session.unwound()


class _Quit(Exception):
    """The user confirmed the quit dialog from the Quit row of the menu.

    A menu round runs a full tool inside itself, so a ``return`` cannot tell that the user
    selected Quit: after a ``return``, the loop shows the menu again. Thus the round
    raises this exception, and the one function that owns the decision to leave catches
    it. The confirm already stored on the context whether to remove the OS bond too
    (refer to :func:`_confirm_quit`).
    """


async def _menu_round(
    ctx: AppContext,
    session: TuiSession,
    menu: SelectScreen,
    tools: list,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Show the menu one time, then run the selection that it committed.

    This function is separate from :func:`_menu_loop`, so that all of a round (the run of
    the tool included) is in one ``try``. Thus an unwind raised at any point in it goes to
    the menu, and does not escape past the loop.

    Args:
        ctx: The shared application context.
        session: The running TUI session.
        menu: The menu screen, which lasts for all of the session (pushed here, and popped
            before the tool runs).
        tools: The menu-visible tools, in the order in which their rows were built.
        loop: The running event loop, for the future of the menu in each round.

    Raises:
        _Quit: If the user confirmed the quit dialog.
        PopToMenu: If the user pressed ^W in what this round ran.
    """
    menu.future = loop.create_future()
    session.push(menu)
    ran_over_menu = False
    try:
        selection = await menu.future
        # The Quit row asks first, in a dialog over the menu (which is still pushed). Cancel
        # ends the round and paints the menu again, and its highlight is still on the row.
        if selection == _QUIT_VALUE:
            if await session.confirm_quit():
                raise _Quit()
            return
        # A ``popup`` tool runs while the menu is still pushed. Thus its prompts and its
        # result show over the menu as modal dialogs, and do not replace the screen.
        tool = next((t for t in tools if t.name == selection), None)
        if tool is not None and tool.popup:
            ran_over_menu = True
            await _run_selection(ctx, selection)
    finally:
        session.pop(menu)
    if not ran_over_menu:
        await _run_selection(ctx, selection)


async def _startup(ctx: AppContext) -> bool:
    """Let the user select a companion device (if necessary), and resume passive monitoring.

    Args:
        ctx: The shared application context, which this function updates with the
            selection.

    Returns:
        ``True`` to open the menu, or ``False`` if the user quit at the startup splash
        (with Esc or the Quit row). In that case, the caller skips the menu loop.
    """
    if not (ctx.mock or ctx.explicit_selection):
        from ..core.connection import probe_device
        from ..core.discovery import DiscoveredDevice, discover_all
        from ..core.spiradio import state_dir
        from .device_picker import prompt_device
        from .logo import load_logo

        baudrate = ctx.profile.baudrate if ctx.profile else 115200
        # Enumerate the serial ports (immediate) and scan for BLE companions (a few
        # seconds), behind the splash spinner. Thus the user sees that the wait is
        # intentional, and not a hang.
        devices = await ctx.ui.busy_startup(
            "Scanning for companion devices…",
            discover_all(ble=True),
            title="Select a companion device",
            banner=load_logo(),
        )
        # The smoke test opens the device. If it succeeds, we keep that live connection and
        # use it again for the session, instead of opening it again (boards often reset at
        # each open).
        probed: dict = {}

        async def verify(device: DiscoveredDevice, pin: str | None = None):
            # ``pin`` is what the PIN dialog of the picker got on a retry. For the first try,
            # use the ``--ble-pin`` from the CLI, if there is one.
            used = pin if pin is not None else ctx.ble_pin
            spi_state = (
                state_dir(ctx.settings.config_dir, device.spi or ctx.spi_wiring_for(device.port))
                if device.is_spi
                else None
            )
            result = await probe_device(device, baudrate=baudrate, pin=used, spi_state=spi_state)
            if result is None:
                return None
            connection, info = result
            probed["device_id"] = device.stable_id
            probed["device"] = connection
            # Keep the PIN that worked. The adopted connection has its own copy, but a
            # reconnect during the session builds a new device from the resolved endpoint of
            # the context. Without this PIN, that device goes back with no PIN, and does all
            # of the "needs a PIN" procedure again with a companion to which we already
            # authenticated. The PIN is kept only for the session: nothing writes a pairing
            # code to disk.
            probed["pin"] = used
            return info

        chosen = await prompt_device(
            ctx.ui, devices, ctx.device_store, verify, ctx.settings.profiles
        )
        if chosen is None:
            return False  # the user quit at the splash: exit, and do not open the menu
        ctx.selected_device = chosen
        if chosen.is_spi:
            # A reconnect builds the device again from the context, so the context must know
            # which radio to use. An SPI profile names its own radio. A bare row is the radio
            # of a board that ships with one (the AIO, the Cap of the Cardputer Zero), which
            # ``--spi`` resolves as the one attached radio.
            ctx.spi_override = True
            ctx.tcp_override = None
            ctx.ble_override = None
            ctx.port_override = None
        elif chosen.is_tcp:
            ctx.tcp_override = chosen.target
            ctx.ble_override = None
            ctx.port_override = None
        elif chosen.is_ble:
            ctx.ble_override = chosen.address
            ctx.tcp_override = None
            ctx.port_override = None
        else:
            ctx.port_override = chosen.port
            ctx.ble_override = None
            ctx.tcp_override = None
        if probed.get("device_id") == chosen.stable_id:
            ctx.adopt_device(probed["device"])
            if probed.get("pin"):
                ctx.ble_pin = probed["pin"]
    # Between the device splash and the first paint of the menu, MeshTerm resumes the
    # background listening, and this step opens the device. The step is slow and silent,
    # and without a card the screen stays blank for a moment. Thus show the skeleton card
    # as a dialog during that gap, on each real link (refer to _busy_over_link).
    async with _busy_over_link(ctx, title="Starting up"):
        failure = await _resume_monitor(ctx)
    if failure is not None:
        await _report_startup_failure(ctx, failure)
    # A device that forgets (a radio bridge with no firmware) may have lost settings that
    # you saved through MeshTerm. Offer to reconcile them on the splash, before the menu
    # paints. This step shows nothing, unless a connected device is different from the
    # stored settings (refer to settings_offer).
    from .settings_offer import offer_remembered_settings

    await offer_remembered_settings(ctx)
    return True


async def _report_startup_failure(ctx: AppContext, failure: Exception) -> None:
    """Show why the device that the command line named did not open, before the menu paints.

    With ``--port``, ``--ble``, ``--tcp``, or ``--profile``, there is no device picker to
    show the reason. Before, MeshTerm did not show it at all: the menu opened on "no
    device", and the reason showed only when a tool opened, as a failure of that tool.

    Args:
        ctx: The shared application context.
        failure: The exception that the start of the device raised.
    """
    from .device_picker import connect_failure_text
    from .logo import load_logo

    notice = connect_failure_text(failure)
    notice.append("\nThe menu opens without a radio; a tool that needs one tries again.")
    await ctx.ui.notify_startup(notice, title="Can't connect yet", banner=load_logo())


async def _resume_monitor(ctx: AppContext) -> Exception | None:
    """Start the background listening and the history recording, which are always on.

    The recording has no switch. The monitor registers its hub subscription first. This
    step does not use the device, it is in the process, and it cannot fail. Thus each
    overheard packet is logged from the time when the device opens. The hub itself (a
    MeshCore client must always listen) starts here immediately, unless the
    ``connect_on_start`` value in the config defers it. In that case, the hub opens when a
    tool first uses the device, and the recording starts then. A failure to start the hub
    is not fatal (the menu opens without a device), but this function returns it, so that
    the caller can show the reason after the startup card closes.

    Args:
        ctx: The shared application context.

    Returns:
        The exception that the start of the hub raised, or ``None`` when the hub started
        or was deferred.
    """
    await ctx.monitor.start()
    # The advert scheduler is safe to run from the start, whatever the connect policy is.
    # Each pass checks for a connected device, and without one it skips silently. Thus the
    # start here never opens the device (and the scheduler waits during a deferred
    # connect).
    await ctx.adverts.start()
    # The Watchtower only listens (rules over hub events), so it is also safe from the
    # start.
    await ctx.watchtower.start()
    # The clock setter acts only when a connection becomes stable (and immediately on a
    # connection that the startup picker already adopted), only when its preference is
    # on, and only in the background. It never opens the device and never changes the
    # screen: it reports in the log.
    await ctx.clock_sync.start()
    # The courier empties the outbox on its own paced schedule. Each pass checks for a
    # connected device, and without one it skips silently, the same as the advert
    # scheduler.
    await ctx.courier.start()
    # The battery poller reads the pack for the battery gauge of the header. It only
    # reads, and it skips silently without a device, so it is also safe to run from the
    # start.
    await ctx.battery.start()
    if not ctx.settings.connect_on_start:
        return None
    failure: Exception | None = None
    try:
        await ctx.events.start()
    except Exception as exc:  # noqa: BLE001 - the caller reports it. It never crashes the menu
        get_logger().warning("couldn't start the radio: %s", exc)
        failure = exc
    # Store the inbound messages from the start, so that the inbox and the unread badge
    # stay current, also before the chat screen opens. When connect on start is off, this
    # step is deferred (like the hub), and the chat screen starts it later. Skip this step
    # when the hub could not open the device: the chat only tries the same connection
    # again, and waits for the same failure a second time.
    if failure is None:
        try:
            await ctx.chat.start()
        except Exception:  # noqa: BLE001 - show it in the header, but do not crash the menu
            pass
    # Also warm the metadata of the map tile source behind the menu. The first access to
    # ``.max_zoom`` resolves the TileJSON over the network. Without this step, the location
    # preview of the Map or the Node detail pays for that round trip on the navigation path
    # (a stall when the page of a node with a location opens). The tile source does not
    # depend on the device link, so this step runs in all connect states. Offline, it fails
    # silently, and the map uses a default zoom.
    _warm_basemap(ctx)
    # Warm the slow session caches (the contacts, the channel-slot probe) behind the menu,
    # now that the link is up. Thus the first open of Chat, Trace, or the Dashboard uses the
    # cache, and does not pay for those round trips on the navigation path. There is one
    # quiet wait after the login, instead of a stall at the first open (refer to
    # DeviceState.prewarm). This step occurs only with a live link: a deferred connect
    # (connect_on_start off) returns above, and warms the caches at the first use instead.
    if ctx.is_connected:
        ctx.devstate.prewarm()
    return failure


def _warm_basemap(ctx: AppContext) -> None:
    """Resolve the shared tile source's TileJSON in the background (best-effort, one time).

    The caller does not wait for the result. The resolve is a blocking network call, so it
    runs in a worker thread, outside the event loop. The function ignores each failure
    (offline, a slow source), and the map then uses its default maximum zoom. Because the
    source is warm, the first location preview of the Map or the Node detail opens from
    the resolved source, and does not stall for the round trip.
    """

    async def _warm() -> None:
        try:
            await asyncio.to_thread(lambda: ctx.basemap_source.max_zoom)
        except Exception:  # noqa: BLE001 - the map works offline at a default zoom
            pass

    asyncio.ensure_future(_warm())


@asynccontextmanager
async def _busy_over_link(ctx: AppContext, *, title: str = "") -> AsyncIterator[None]:
    """Show the skeleton-card overlay during the wrapped block, on each real device link.

    Bluetooth is the slowest, but the navigation on serial also has a lag that the user
    can see. Thus the overlay is installed for each transport. Only the mock simulator
    does not use it (it has no link and answers immediately, so ``active_transport`` is
    ``None``). The overlay has its own delay before it shows, so a quick operation does
    not cause a flash. Thus the overlay never flickers.

    Args:
        ctx: The shared application context (read for the active transport and UI surface).
        title: An optional heading for the card, which names the screen whose data
            MeshTerm reads.
    """
    if ctx.active_transport is not None:
        async with ctx.ui.busy_overlay(_reading_caption(ctx), title=title):
            yield
    else:
        yield


def _reading_caption(ctx: AppContext) -> str:
    """The caption of the skeleton card: "reading from <companion>…", or only "reading…".

    The caption names the selected companion when this session knows one. Thus the user
    sees the wait as a read from a specific device, not as a stall with no cause.
    """
    device = ctx.selected_device
    if device is not None and device.label:
        return f"reading from {device.label}…"
    return "reading…"


async def _run_selection(ctx: AppContext, name: str) -> None:
    """Get the parameters for one tool from the menu, run the tool, and show its result.

    This function does not handle a lost device link. The watcher for the full session
    (refer to :func:`_session_loop`) polls independently, and opens the reconnect dialog
    on any screen of the session. Thus, on a connection-lost error, this function removes
    the half-built output of the tool and returns. The watcher continues from there,
    within one poll interval.

    Args:
        ctx: The shared application context.
        name: The name of the selected tool.
    """
    from ..core.connection import is_connection_lost
    from ..core.selection import DeviceSelectionError
    from ..tools import get_tool
    from .device_picker import connect_failure_text

    tool = get_tool(name)
    if tool is None:  # pragma: no cover - the registry and the menu always agree
        ctx.ui.note(f"[err]Unknown tool: {name}[/err]")
        await ctx.ui.present(title=name)
        return

    title = tool.title or tool.name
    try:
        # When a tool opens, the frame can stay blank while the companion answers (the menu
        # is popped, and the first prompt is not pushed yet). This is very visible over
        # Bluetooth, and the user can see it on serial too. Show the skeleton card during
        # that gap, so that the user sees the wait as work, not as a hang. The overlay
        # paints only between screens, so the prompts show through it.
        async with _busy_over_link(ctx, title=title):
            params = await tool.prompt_params(ctx)
            if params is None:  # the user cancelled a prompt
                ctx.ui.discard()
                return
            result = await tool.execute(ctx, params)
    except Exception as exc:  # noqa: BLE001 - show errors, and do not crash the menu
        # A device that never opened cannot be lost, whatever its error says. Also, the
        # watcher has no connection to watch, so it never asks about it. Thus this function
        # shows that case here, in the sentence of the connection, and does not discard it.
        never_opened = isinstance(exc, DeviceSelectionError)
        if is_connection_lost(exc) and not never_opened:
            ctx.ui.discard()  # remove the half-built output. The watcher will ask to reconnect.
            return
        # A tool that failed is a caution, not a loss: the run is over, nothing that it was
        # going to do is half-done, and the next step of the user is to try again. Thus it
        # gets the amber tone, not the reserved red (the same tier as a danger dialog). The
        # message dialog reads that tone from the note to set its border (refer to
        # :func:`~meshterm.ui.tui.session._message_border`).
        if never_opened:
            reason = Text("⚠ ", style="warn")
            reason.append_text(connect_failure_text(exc))
            ctx.ui.show(reason)
        else:
            # Escaped: an error has the words of a library. If MeshTerm reads
            # `[org.bluez.Error.Failed]` as markup, it is a tag that silently removes the
            # most useful part of the error.
            ctx.ui.note(f"[warn]⚠ {title} failed:[/warn] {escape(str(exc))}")
        await ctx.ui.present(title=title)
        return

    if result.message:
        ctx.ui.note(result.message)
    for artifact in result.artifacts:
        ctx.ui.note(f"[ok]●[/ok] wrote [accent]{artifact}[/accent]")
    await ctx.ui.present(title=title)


async def _session_loop(ctx: AppContext, session: TuiSession) -> None:
    """Run the menu with one watcher, always on, that handles a disconnect on any screen.

    The menu loop runs as a worker that can be cancelled, next to one liveness watcher for
    the full session. The first of the two to finish wins:

    - If the user quits, the menu loop returns, and the watcher stops.
    - If the device is disconnected, on any screen (a tool prompt, the chat screen, the
      map, or the idle menu), the watcher fires. The work in progress is cancelled, the
      screen stack is unwound, and the reconnect dialog shows.

    After a successful reconnect, the loop starts a new menu. After a quit, it returns.

    Args:
        ctx: The shared application context.
        session: The running TUI session.
    """
    while True:
        worker = asyncio.ensure_future(_menu_loop(ctx, session))
        watcher = asyncio.ensure_future(_wait_for_disconnect(ctx))
        done, _ = await asyncio.wait({worker, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await _cancel_and_wait(watcher)
            worker.result()  # the user quit (or re-raise a menu-loop error)
            return
        # The device link is lost. Stop the work of the menu, clear the screens that it
        # left, and ask the user to reconnect or quit, over a clean frame.
        await _cancel_and_wait(worker)
        session.reset()
        if await _handle_disconnect(ctx, session):
            return  # the user selected Quit
        # Reconnected: loop, and start a new menu (with a new watcher).


async def _cancel_and_wait(task: asyncio.Future) -> None:
    """Cancel ``task`` and await its unwind, and ignore the cancellation and each error.

    MeshTerm uses this function to stop the other task of the pair (the menu worker or the
    watcher). When this function awaits the cancelled task, the ``finally`` blocks of
    that task run (and pop the screens that it pushed) before we continue. The error of
    the task is no longer important (the link is gone), so this function ignores it
    intentionally. The await also gets the result of a task that is already finished, so
    asyncio does not log an unwanted "exception never retrieved" warning.
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - the error of a cancelled worker is not important
        pass


async def _wait_for_disconnect(ctx: AppContext) -> None:
    """Resolve when the transport link of the connected companion is lost.

    This function polls a fast liveness check that has no side effects
    (:meth:`AppContext.link_alive`), and does not wait for a failed command. After a
    serial disconnect, the ``meshcore`` client continues to give cached data and never
    raises an error (this was confirmed on hardware). Thus an active liveness check is the
    only reliable way to find a disconnected cable. The same check covers Bluetooth:
    there, it reads the connection flag of the BLE client (which changes immediately when
    the peripheral disconnects or goes out of range). The function never resolves for the
    simulator (it cannot be disconnected), or before a real device opens.

    Args:
        ctx: The shared application context (read for the connection state and liveness).
    """
    if ctx.mock:
        await asyncio.Event().wait()  # the simulator is never "unplugged": wait forever
        return
    # A reboot that we sent is a disconnect that we already know about. Race it against
    # the poll, so that the reconnect dialog opens when the command goes out. Without the
    # race, the dialog opens only when a poll occurs during the reboot, if a poll does.
    announced = asyncio.ensure_future(ctx.link_going_down())
    polled = asyncio.ensure_future(_poll_for_disconnect(ctx))
    try:
        await asyncio.wait({announced, polled}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        await _cancel_and_wait(announced)
        await _cancel_and_wait(polled)


async def _poll_for_disconnect(ctx: AppContext) -> None:
    """Resolve when the liveness check finds the link gone, debounced against a short gap."""
    while True:
        await asyncio.sleep(_LIVENESS_POLL_S)
        if not ctx.is_connected:
            continue  # nothing connected to watch yet (a deferred connect, or no device)
        if await ctx.link_alive():
            continue
        await asyncio.sleep(_LIVENESS_CONFIRM_S)  # debounce a short enumeration or link gap
        if not await ctx.link_alive():
            return


async def _handle_disconnect(ctx: AppContext, session: TuiSession) -> bool:
    """Show a reconnect dialog, watch for the device to return, and resume when it returns.

    This function shows a centred dialog in the warn style
    (:class:`~meshterm.ui.tui.prompt.ReconnectDialog`), with an animated spinner and one
    Quit button. Two background tasks run behind it:

    - One task animates the spinner.
    - The other task waits until the device is present again (for serial, until its port
      enumerates again: refer to :func:`_auto_reconnect`). Then it tries
      :meth:`~meshterm.context.AppContext.reconnect` again and again, until one try
      succeeds. A board can enumerate again a moment before it answers, so after a failed
      try, the task only tries again.

    The first result wins. A successful reconnect closes the dialog and resumes the
    session. When the user presses Quit (or Enter), MeshTerm quits. The spinner shows all
    of the time.

    Args:
        ctx: The shared application context.
        session: The running TUI session.

    Returns:
        ``True`` if the user selected Quit, or ``False`` after the device reconnected.
    """
    # The config editor announced this disconnect (it sent a reboot command), so it is
    # expected. Thus the dialog tells what occurs, and does not suggest that a cable was
    # disconnected. This function clears the flag here, so that a later, real disconnect
    # gets the general wording again.
    rebooting = ctx.take_reboot()
    if rebooting:
        dialog = ReconnectDialog(
            "Rebooting — waiting for the device to come back…",
            title="Device rebooting",
        )
    else:
        dialog = ReconnectDialog("Waiting for your device — reconnect it to resume.")
    dialog.future = asyncio.get_running_loop().create_future()
    # The session stack was cleared before this call (refer to _session_loop). Thus push a
    # base screen for the dialog to show over. A floating screen with nothing under it is
    # drawn as the base (with framed chrome and no centred panel), not as a box. The base
    # gives the dialog something to be centred over, horizontally and vertically. The base
    # is the menu that the user was on (it is still declared as the root), so the dialog
    # looks like an interruption of the page, not like an empty page. A session that
    # never got to the menu gets an empty screen.
    base = session.root or ScrollScreen("", floating=False, footer_hint="")
    session.push(base)
    session.push(dialog)
    animator = asyncio.ensure_future(_animate_dialog(session, dialog))
    reconnector = asyncio.ensure_future(
        _auto_reconnect(ctx, dialog, settle_s=_REBOOT_SETTLE_S if rebooting else 0.0)
    )
    try:
        result = await dialog.future
    finally:
        await _cancel_and_wait(reconnector)
        await _cancel_and_wait(animator)
        session.pop(dialog)
        session.pop(base)
    return result == "quit"


async def _animate_dialog(session: TuiSession, dialog: ReconnectDialog) -> None:
    """Move the spinner of the reconnect dialog, and paint at a steady rate until cancelled."""
    while True:
        await asyncio.sleep(spinner_interval())
        dialog.tick()
        session.invalidate()


async def _auto_reconnect(
    ctx: AppContext, dialog: ReconnectDialog, *, settle_s: float = 0.0
) -> None:
    """Poll for the device to return, reconnect when it returns, then close ``dialog``.

    First, this function releases the dead link
    (:meth:`~meshterm.context.AppContext.release_link`), so that the peripheral can send
    its BLE advertisement while we wait for it. Then each transport waits for its own
    proof that the device is reachable, before it uses the device. Thus each try is made
    only against a device that is present:

    - Serial waits for the OS to enumerate again the port on which the connection opened.
    - Bluetooth listens for the BLE advertisement of the companion
      (:func:`~meshterm.core.discovery.find_ble_device`). This is the same question, asked
      of the only registry that BLE has. Then it gives the live handle that it found to
      :meth:`~meshterm.context.AppContext.reconnect`. After a power cycle, the peripheral
      is a new one for the OS. Thus the scan is how we find that it came back, and also
      how we get a handle that can open it. Before, this function tried again without a
      scan, and that was wrong in two ways. It sent the internal address lookup of bleak
      to a device that sent no BLE advertisement (a slow failure, which overlapped the
      time window in which the device came back). And when a try finally succeeded, it
      used the dead handle from the startup scan.
    - TCP has no such probe, and only tries the connect again. The connect fails quickly
      when the endpoint is absent.

    A failed try (the device is back but the board is not ready yet, or the device was
    lost again) loops and tries again. After :data:`_REASON_AFTER_FAILURES` tries fail in
    sequence, the dialog shows the reason under its spinner, in the sentence of the
    connection. At the first success, the future of the dialog is resolved with
    ``"reconnected"``, and this closes the dialog. The function runs until it succeeds or
    the task is cancelled (when the user quits).

    Args:
        ctx: The shared application context.
        dialog: The reconnect dialog to close when the link is back.
        settle_s: How long to wait after the release of the link, before the first try.
            It is not zero only for a reboot that we sent. The watcher fires immediately
            when the command goes out. Without this wait, MeshTerm opens again a serial
            port that stayed present, against a board that has not started its restart
            yet.
    """
    from ..core.connection import serial_port_present
    from ..core.discovery import find_ble_device
    from .device_picker import connect_failure_text

    failures = 0
    last_reason = ""
    waiting_message = dialog.message

    def gone_again() -> None:
        # The device is gone again, so the last refusal is no longer important. Go back to
        # the wait, until the device returns and refuses again (or does not).
        nonlocal failures
        if failures:
            failures = 0
            dialog.set_message(waiting_message)
            dialog.set_detail(None)

    # Release the dead link before you look for the device. A Bluetooth companion to which
    # we are still connected in name sends no BLE advertisement. Without the release, the
    # scan below waits for a peripheral that is on and silent only because we did not
    # release it.
    await ctx.release_link()
    if settle_s:
        await asyncio.sleep(settle_s)

    while True:
        ble_device: object | None = None
        transport = ctx.active_transport
        if transport == "serial":
            port = ctx.active_port
            # Outside the event loop: the probe enumerates ports, and this blocks for long
            # enough to stop the spinner that this dialog shows while it waits.
            if port is not None and not await asyncio.to_thread(serial_port_present, port):
                gone_again()
                await asyncio.sleep(_LIVENESS_POLL_S)
                continue
        elif transport == "ble":
            address = ctx.active_address
            if address is not None:
                ble_device = await find_ble_device(address, timeout=_BLE_PRESENCE_SCAN_S)
                if ble_device is None:
                    # The scan window is itself the wait (it listened for several seconds),
                    # so only a short pause is necessary. The pause stops a fast loop when a
                    # scan fails immediately (Bluetooth switched off during the session).
                    gone_again()
                    await asyncio.sleep(_BLE_RESCAN_PAUSE_S)
                    continue
        try:
            await ctx.reconnect(ble_device=ble_device)
        except Exception as exc:  # noqa: BLE001 - not reachable yet: keep the dialog, try again
            # A board can refuse one time while it boots, so after the first failure, the
            # task only tries again. A failure that continues shows under the spinner (for
            # example, another program holds the port, or a pairing is old). If not, the
            # user waits for it forever. The log has one line for each reason, not one line
            # for each try.
            failures += 1
            reason = connect_failure_text(exc)
            if reason.plain != last_reason:
                last_reason = reason.plain
                get_logger().warning("reconnect failed: %s", last_reason)
            if failures >= _REASON_AFTER_FAILURES:
                dialog.set_message("Still trying to reconnect…")
                dialog.set_detail(reason)
            await asyncio.sleep(_LIVENESS_POLL_S)
            continue
        dialog.resolve("reconnected")
        return


# Exposed for the tools that must have a standalone console outside a context (rare).
default_console = make_console
