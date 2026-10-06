# SPDX-License-Identifier: Apache-2.0
"""The persistent full-screen session: the central part of the TUI library.

A :class:`TuiSession` owns one prompt_toolkit :class:`Application`, a stack of
:class:`~meshterm.ui.tui.screen.Screen` layers, and the persistent header and footer. It
does these tasks:

- It changes the prompt_toolkit key events into normalized *actions*, and it sends each
  action to the top screen.
- It composes the current frame through :mod:`~meshterm.ui.tui.frame`. The frame stays in
  the limits of the terminal, and its content flows again when the terminal changes size.
- It gives ``async`` helpers (``select``/``text``/``confirm``/``autocomplete``/``scroll``/
  ``progress``). Each helper pushes a screen, awaits its result, and pops it. This push,
  await, and pop model is behind each prompt.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext, suppress
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, Layout, Window
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.utils import get_cwidth
from rich.console import RenderableType
from rich.text import Text

from ...core import win32dll
from ...persistence.logging import get_logger
from ...platforms import get_platform
from ...services import hold_to_quit, modifier_watch
from . import colsnap, fastrender, fkeys, frame
from .emoji_width import ClusterTextControl
from .holdquit import EscHoldWatch, Quiet, WatchedEsc
from .overlay import BusyOverlay
from .progress import TuiProgress
from .prompt import (
    AutocompleteScreen,
    ButtonDialog,
    ConfirmScreen,
    PinDialog,
    TextScreen,
    TypedConfirmDialog,
    Validator,
)
from .screen import CANCEL, POP_ALL, BusyDialog, BusyScreen, PopToMenu, Screen, ScrollScreen
from .select import Choice, ReorderScreen, SelectScreen, Separator
from .spinner import spinner_interval

#: Each Ctrl-letter chord that the app binds, indexed by the bare lowercase letter. This
#: table is the single source of truth for the two halves of the life of a chord:
#:
#: - the ``Keys.Control*`` bindings, which are folded into :data:`_KEY_ACTIONS` below.
#: - the right-Ctrl rescue in :meth:`TuiSession._dispatch` (refer to
#:   :func:`_right_ctrl_down`). When a keyboard layout claims the right Ctrl key, that key
#:   sends a bare letter as text. The rescue changes that letter back into the chord.
#:
#: Add a chord here, and the two Ctrl keys get it. A second edit is not necessary, and no
#: chord works on only one side of the keyboard. ``quit`` and ``paste_clipboard`` belong to
#: the session (``_dispatch`` answers them). The other entries are actions that the session
#: sends to the top screen. While the right Ctrl key is held, a bare letter gets to the text
#: only when the layout has no third-level glyph for that key (that is, when the key press is
#: the chord). A real third-level character arrives as a different glyph, and it never
#: matches this table.
#: ``m``/``i``/``h`` are not free: prompt_toolkit spells Enter, Tab, and Backspace as
#: ``Keys.ControlM``/``ControlI``/``ControlH``. If this table claims them, it binds those
#: keys again.
#: The letter of a chord is the mnemonic of the action, not of the word that one screen uses
#: for it. ``locate`` is ^Y for **you** (JP, 2026-08-09). It is the same keyboard key on the
#: map and in the mesh walk, because on both it names our node. It was ^U until 2026-10-01.
#: Then U went to ``url_code``: the links of a chat message as QR codes.
#:
#: Two of these chords are for the full app, not for one screen:
#:
#: - ``to_menu`` (^W) unwinds the full navigation stack back to the main menu.
#: - ``quit`` (^Q, and also the older ^C) asks to quit the app from any screen. A second
#:   press while the question is open quits (refer to :meth:`TuiSession.request_quit`).
#:
#: No footer hint and no F-key chip shows these two chords. The F-key lane has three free
#: slots for each screen, and a chip for a global verb uses one slot on each screen permanently
#: (JP, 2026-08-30). The only exception is ``^Q quit?`` in the hint of the main menu. There
#: the Esc key does nothing, and the user looks for the way out (issue #22). We chose ^W
#: because users press it by reflex to close everything. On the Canadian Multilingual layout,
#: we checked that the keyboard sends the two chords (^W ``U+0017``, ^Q ``U+0011``), and the
#: terminal does not consume either of them. The raw mode of prompt_toolkit clears
#: ``IXON``/``IXOFF``, so ^Q is not XON here.
_CTRL_LETTER_CHORDS: dict[str, str] = {
    "c": "quit",
    # ^L logs in to the room that a room screen shows.
    "l": "login",
    "p": "paths",
    "q": "quit",
    "r": "retry",
    # ^S is the XOFF key. It is safe here for the same reason that ^Q is not XON (refer to
    # the text above): the raw mode clears IXON/IXOFF. Thus the terminal never takes it to
    # freeze the screen.
    "s": "reveal",
    "u": "url_code",
    "v": "paste_clipboard",
    "w": "to_menu",
    "y": "locate",
}

#: Maps the prompt_toolkit keys to the normalized action names that the screens understand.
_KEY_ACTIONS: dict[Any, str] = {
    Keys.Up: "up",
    Keys.Down: "down",
    Keys.Left: "left",
    Keys.Right: "right",
    Keys.ShiftUp: "shift_up",
    Keys.ShiftDown: "shift_down",
    Keys.ShiftLeft: "shift_left",
    Keys.ShiftRight: "shift_right",
    Keys.PageUp: "pageup",
    Keys.PageDown: "pagedown",
    Keys.Home: "home",
    Keys.End: "end",
    Keys.ControlHome: "ctrl_home",
    Keys.ControlEnd: "ctrl_end",
    Keys.ControlPageUp: "ctrl_pageup",
    Keys.ControlPageDown: "ctrl_pagedown",
    Keys.ControlLeft: "ctrl_left",
    Keys.ControlRight: "ctrl_right",
    Keys.ControlUp: "ctrl_up",
    Keys.ControlDown: "ctrl_down",
    Keys.Enter: "enter",
    # Ctrl+Enter, as well as a terminal can spell it. No such keycode exists: Enter is ^M.
    # Thus a console without modifyOtherKeys has nothing more to modify, and it sends a
    # plain CR. Most consoles send LF (``ControlJ`` in prompt_toolkit). LF has no other
    # binding here (it is an unprintable character, and ``_typed`` removes it). Thus this
    # binding costs nothing, and it works on the terminals that send LF. The right-Ctrl
    # rescue below covers the other cases through _CTRL_CHORDS. On a console that spells
    # neither, the chip on the F-key lane is the affordance (that is already true on the
    # platform of that console).
    Keys.ControlJ: "ctrl_enter",
    Keys.Escape: "escape",
    Keys.Backspace: "backspace",
    Keys.Delete: "delete",
    Keys.Tab: "tab",
    Keys.BackTab: "shift_tab",
    # The function keys, for the F-key lane of a handheld. This table binds all of them,
    # because the deck of the platform decides which keycodes drive the lane, not this
    # table. The MCU of the PicoCalc sends its Shift bank as F6–F10. The emulator of the
    # Cardputer sends Shift+F4..F8 the same as xterm, and prompt_toolkit reads them as
    # F16–F20. In _dispatch, the session resolves them against the lane of the top screen.
    # A key that the deck does not use, or a key on a platform without a lane, resolves to
    # nothing and is ignored. On the PicoCalc, Alt+Fn is the VT switch of the kernel, and
    # it must never have a binding.
    **{getattr(Keys, f"F{n}"): f"f{n}" for n in range(1, 25)},
    # The Ctrl-letter chords, made from the one table above. Thus a chord can never have a
    # binding without its right-Ctrl rescue (or a rescue into an action that nothing binds).
    **{
        getattr(Keys, f"Control{letter.upper()}"): action
        for letter, action in _CTRL_LETTER_CHORDS.items()
    },
}

#: The plain actions that have a related Ctrl chord, for the right-Ctrl rescue in
#: :meth:`TuiSession._dispatch` (refer to :func:`_right_ctrl_down`). These are the
#: navigation keys, and also ``enter``. A terminal cannot reliably spell ``enter`` as a chord
#: (refer to ``Keys.ControlJ`` above). Thus, for ``enter``, the rescue is not a fallback: it
#: is the more reliable of the two ways.
_CTRL_CHORDS: dict[str, str] = {
    "up": "ctrl_up",
    "down": "ctrl_down",
    "left": "ctrl_left",
    "right": "ctrl_right",
    "home": "ctrl_home",
    "end": "ctrl_end",
    "pageup": "ctrl_pageup",
    "pagedown": "ctrl_pagedown",
    "enter": "ctrl_enter",
}


def _right_ctrl_down() -> bool:
    """Whether the right Ctrl key is physically held now (Windows, ``False`` on other systems).

    This function is the rescue behind the right-Ctrl chords. Some keyboard layouts claim the
    right Ctrl key as a modifier for a character group. (The Canadian Multilingual Standard
    uses it to get to a third character level.) These layouts can send an arrow with the
    right Ctrl key to the console as a bare arrow. Then no Ctrl flag stays for prompt_toolkit
    to map, so only the left Ctrl key could control a sortable list. This function probes the
    physical state of the key (``GetAsyncKeyState``). It does not trust the event modifiers,
    because the layout removed them. If an arrow got to our dispatch, our terminal had the
    focus. Thus a right Ctrl key that is held at that instant is a chord that the user makes.
    The platforms other than Windows report ``False``. A VT terminal itself encodes the Ctrl
    modifier the same for the two sides, and there is no key state for each side to examine.
    """
    if sys.platform != "win32":
        return False
    try:
        return bool(win32dll.user32().GetAsyncKeyState(0xA3) & 0x8000)  # VK_RCONTROL
    except Exception:  # noqa: BLE001 - if the probe fails, the plain action stays
        return False


def _read_clipboard() -> str:
    """Try to read the Unicode text of the OS clipboard (Windows, ``""`` on other systems).

    This function is the fallback behind Ctrl-V on a terminal that sends the key as it is,
    instead of as a bracketed paste (refer to :meth:`TuiSession._dispatch`). It gets the text
    of the clipboard directly from the Win32 API. Each failure gives ``""``: a platform that
    is not Windows, an empty clipboard, a clipboard without text, or a clipboard that another
    program uses, so that we could not open it. Thus the paste does nothing, and no exception
    goes into the key handler. The calls that return a pointer declare a ``c_void_p`` result,
    so that a 64-bit handle is not truncated to an int.
    """
    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        CF_UNICODETEXT = 13
        # Private handles. Without them, the argtypes below are declared on objects that all
        # the libraries in the process share. Refer to meshterm.core.win32dll.
        user32 = win32dll.user32()
        kernel32 = win32dll.kernel32()
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001 - a small clipboard problem must never break a key press
        return ""


def _reclaim_last_column() -> bool | None:
    """Whether to use the last column of the terminal, or ``None`` to ask the terminal.

    The Windows console output of prompt_toolkit reports the window as one column narrower
    than it is. It does this on purpose (refer to :func:`_probe_hides_last_column`). Thus the
    frame is drawn to ``columns - 1``, and the real last column is not used. The user can see
    that column at the right of the border, and can select it. When this value is on,
    :class:`_WidthExtendedOutput` tells the renderer and the frame compositor that the window
    is one column wider. Then that last column is drawn.

    This is correct only where the probe hid the column. In all other cases, the extra column
    is outside the real screen. prompt_toolkit draws with autowrap off, so the phantom last
    cell of each row is written over the real cell before it. Thus the text at the right edge
    loses its last character. On Linux, this cut the battery gauge of the header: a full
    battery showed ``10`` instead of ``100``, and 99% showed ``9%``. Thus the default is a
    question, not an answer. ``None`` means "reclaim if, and only if, the output this session
    draws on is one whose probe hides the column". The session decides this when that output
    exists.

    Three sources give the answer, in the order of confidence, because the user at the
    keyboard can see the column and the code can only infer it:

    1. ``MESHTERM_FULL_WIDTH=0``/``=1``, for one run.
    2. The ``full_width`` preference (``yes``/``no``). ``auto``, the default, gives no
       answer.
    3. The platform. It answers ``False`` where its console is exact
       (:data:`~meshterm.platforms.PICOCALC_LYRA`). In the other cases, it lets the terminal
       decide.

    The function reads the values again at each call (never at import time, into a cache).
    Thus it agrees with the platform that :func:`~meshterm.platforms.set_platform` installed
    for this process. Refer to the docstring of that module for the reason why a cached or
    imported copy of the platform becomes old.
    """
    from ...core.preferences import current as current_preferences

    override = os.environ.get("MESHTERM_FULL_WIDTH")
    if override is not None:
        return override != "0"
    preferred = current_preferences().full_width
    if preferred != "auto":
        return preferred == "yes"
    return None if get_platform().width_reclaim else False


def _probe_hides_last_column(output: Any) -> bool:
    """Whether prompt_toolkit gives ``output`` a width one column narrower than its window.

    True for its Windows console output, and for no other output. ``Win32Output.get_size``
    takes the visible width as ``srWindow.Right - srWindow.Left``. This is an inclusive span,
    so it is one column short. Then the function limits the width to less than the right
    margin of the buffer ("windows will wrap otherwise"). ``Windows10_Output`` and
    ``ConEmuOutput`` both use the size of the ``Win32Output`` that they hold. The
    ``Vt100_Output`` of a POSIX terminal reads ``TIOCGWINSZ``, which is exact. Thus there is
    no column to reclaim there.
    """
    if sys.platform != "win32":
        return False  # the win32 module of prompt_toolkit asserts the platform on import
    from prompt_toolkit.output.win32 import Win32Output

    return isinstance(output, Win32Output) or isinstance(
        getattr(output, "win32_output", None), Win32Output
    )


#: The depth for each value of the ``color_depth`` preference. ``auto`` is not in the table
#: on purpose: it is the only value that is not a depth, and :func:`_color_depth` resolves it
#: separately.
_DEPTH_BY_NAME = {
    "truecolor": ColorDepth.DEPTH_24_BIT,
    "256": ColorDepth.DEPTH_8_BIT,
    "16": ColorDepth.DEPTH_4_BIT,
}

#: The ``COLORTERM`` values of a terminal that tells the truth about 24-bit colour. There is
#: no in-band way to ask. A terminal without truecolor answers a truecolor SGR with an
#: approximation, and it does not tell us. From here, that looks exactly the same as a
#: terminal with truecolor. Thus this convention, which each truecolor terminal follows, is
#: all the evidence.
_TRUECOLOR_COLORTERM = frozenset({"truecolor", "24bit"})


def _color_depth() -> ColorDepth | None:
    """How many colours to send to this terminal, or ``None`` to keep the prompt_toolkit verdict.

    prompt_toolkit chooses a depth for each *output class*, and its two classes do not agree.
    ``Windows10_Output`` returns ``TRUE_COLOR`` directly. ``Vt100_Output`` returns
    ``DEPTH_8_BIT`` for each ``TERM`` except ``linux`` and a dumb terminal. Thus the same
    build of MeshTerm, with the same theme, has 24-bit colour on Windows and 256 colours on
    macOS and Linux. The quantization is silent, and for this reason nobody saw it: the app
    looks correct, only flatter. But the quantization damages exactly the thing that this
    palette is made of. The seven ``heat.*`` steps and the hue wheel for the nodes
    (:func:`~meshterm.ui.theme.node_style`) are pastels that are near each other, and we
    chose them so that the user can tell them apart. In the 216-colour cube, neighbours become
    the same colour, and the user cannot tell the identities apart by hue. prompt_toolkit
    does not read ``COLORTERM``. It reads only ``PROMPT_TOOLKIT_COLOR_DEPTH``. Thus a
    terminal that announces truecolor in the usual way still gets 256 colours.

    Three sources give the answer, in the order of confidence, the same as in
    :func:`_reclaim_last_column`:

    1. ``MESHTERM_COLOR_DEPTH``, for one run.
    2. The ``color_depth`` preference.
    3. A reading of the environment.

    The reading only increases the verdict. It never decreases it. If a terminal does not
    give a positive claim, the function returns ``None``, and the terminal keeps exactly the
    depth that it had. Thus this function cannot make a host worse when it does not know that
    host. This is more important than a correct answer everywhere. If the function guesses
    24-bit for a terminal without 24-bit colour, the result is not a duller palette: it is no
    colour at all.

    Returns:
        The depth to force, or ``None`` to keep the default of prompt_toolkit.
    """
    from ...core.preferences import current as current_preferences

    override = os.environ.get("MESHTERM_COLOR_DEPTH")
    preferred = override if override is not None else current_preferences().color_depth
    if preferred in _DEPTH_BY_NAME:
        return _DEPTH_BY_NAME[preferred]
    if preferred != "auto":
        return None
    # A 16-slot console is already correct, and it has no truecolor to claim. The theme
    # addresses its palette by index (refer to theme._VT_SLOTS). If the function increases
    # the depth, it sends RGB to a screen that has only sixteen colours for it.
    if not get_platform().truecolor:
        return None
    if os.environ.get("COLORTERM", "").strip().lower() in _TRUECOLOR_COLORTERM:
        return ColorDepth.DEPTH_24_BIT
    # The terminfo spelling for a direct-colour entry (``xterm-direct``, ``*-direct16m``).
    if os.environ.get("TERM", "").endswith(("-direct", "-direct16m")):
        return ColorDepth.DEPTH_24_BIT
    return None


#: The maximum number of dialog layers that the layout can float over the background at one
#: time. It is a fixed pool of floats, each one a centred box (refer to
#: :meth:`TuiSession._build_app`). The pool is much larger than the deepest real nesting: the
#: list of a tool, a detail dialog for an item, and a confirm over it are only three.
_MAX_DIALOG_LAYERS = 8

#: How long :meth:`TuiSession.leave` waits for the quiesce of the app to start (that is, for
#: a transmission in progress to finish). After this time, the method continues all the same.
#: On the Cardputer Zero, the full exit must fit in the three seconds that the launcher gives
#: between SIGTERM and SIGKILL (:data:`~meshterm.services.hold_to_quit.GRACE_S`). The rest of
#: the exit took approximately one second there (measured on 2026-10-06).
QUIESCE_WAIT_S = 1.0

_log = get_logger()


def _changed_rows(before: str, after: str) -> list[int] | None:
    """Which lines of a full-screen frame are different, or ``None`` if a comparison fails.

    The two frames are composed with one line for each terminal row (refer to
    :func:`~meshterm.ui.tui.frame.compose_base`). Thus a line index is also a row index, and
    :meth:`TuiSession._scrub_rows` uses this mapping. If the line counts are different, the
    height of the frame changed (a resize, or a splash that gives its place to the framed
    layout), and this mapping is not true. Then the caller does a full paint.
    """
    old, new = before.split("\n"), after.split("\n")
    if len(old) != len(new):
        return None
    return [i for i, (a, b) in enumerate(zip(old, new, strict=True)) if a != b]


def _has_wide_glyph(text: str) -> bool:
    """Whether ``text`` has a glyph for which prompt_toolkit reserves two cells.

    The terminal and the renderer can disagree about a width-2 glyph. The usual case here is
    an emoji (``👋``) that the terminal draws in one cell. pt reserves two cells, the
    terminal moves forward one cell, and after that point the cursor model of the row is
    incorrect. The node-type marks (``▲●■``) and the braille of a chart have a width of 1
    everywhere, so they never cause a ``True`` result. The ``>= 0x1100`` guard skips the
    ASCII and Latin characters, which are most of a frame, before the width lookup. This is
    important, because this function examines the full composed frame at each paint.

    On a platform that folds its frames to a fixed font, the answer is always ``False``.
    That font is a set of 512 glyphs with no wide glyph in it. Thus nothing that a composed
    frame can contain can return ``True``. The function answers from the platform instead of
    from the text. Thus it skips a scan of each character of the full frame at each paint.
    It also skips the scrub machinery that a ``True`` result arms, so the cheap differential
    paint always stays in use.

    The fold gives this guarantee, not the icon table. A terminal that cannot draw emoji (the
    classic Windows console, refer to :func:`meshterm.ui.termfont.emoji_support`) still
    renders the rest of the BMP. A contact with a CJK name is two cells wide there, the same
    as everywhere else. If this function reads ``emoji`` instead, it skips the scan on a frame
    that must have it, and leaves stray characters in the row.
    """
    if get_platform().font:
        return False
    return any(ord(ch) >= 0x1100 and get_cwidth(ch) == 2 for ch in text)


class _WidthExtendedOutput:
    """A prompt_toolkit ``Output`` proxy that reports one more terminal column.

    The proxy wraps the real output, and forwards all calls to it with no change, except
    :meth:`get_size`, which adds a column. The full render pipeline uses
    ``output.get_size()``: the differential renderer of prompt_toolkit, and the frame
    compositor of MeshTerm (through :meth:`TuiSession._size`). Thus this one override makes
    the two use the reclaimed column together. Refer to :func:`_reclaim_last_column` for when
    this is correct (and when it is not).
    """

    def __init__(self, inner: Any) -> None:
        """Wrap ``inner`` (the concrete prompt_toolkit output for the real terminal)."""
        self._inner = inner

    def get_size(self) -> Size:
        """The wrapped size with one more column, so that the last column is used."""
        size = self._inner.get_size()
        return Size(rows=size.rows, columns=size.columns + 1)

    def __getattr__(self, name: str) -> Any:
        """Forward each other attribute or method directly to the wrapped output."""
        return getattr(self._inner, name)


def _message_border(message: Text | str) -> str:
    """Choose the border style of a message dialog from the strongest tone in the text.

    A note about a result has its severity as theme spans (``[err]``, ``[warn]``, ``[ok]``).
    Thus the border of the dialog can show the same severity. An error message gets the
    ``err`` border. A warning (for example "device rebooting") gets the caution border. All
    the other messages, also the success messages, get the standard accent. A plain string
    has no spans, and it is always neutral.

    Args:
        message: The message of the dialog, with styles or plain.

    Returns:
        The Rich style name for the border of the dialog.
    """
    if isinstance(message, Text):
        styles = {str(span.style) for span in message.spans}
        if "err" in styles:
            return "err"
        if "warn" in styles:
            return "warn"
    return "accent"


class Visit:
    """The stay of one screen on the stack, which :meth:`TuiSession.stay` returns.

    It holds only the session and the screen. :meth:`result` arms a new future on that screen
    and awaits it, and the screen stays exactly where it is. A second call is the next round
    of the same visit. This is the loop shape that a hub screen must have.
    """

    __slots__ = ("_session", "_screen")

    def __init__(self, session: TuiSession, screen: Screen) -> None:
        """Bind a visit to its session and screen, and arm the first round of the screen."""
        self._session = session
        self._screen = screen
        self._arm()

    @property
    def screen(self) -> Screen:
        """The screen of this visit (the same object for the whole stay)."""
        return self._screen

    def _arm(self) -> None:
        """Give the screen a new future to resolve into.

        A visited screen is always armed, from the moment when it is pushed until the end of
        the visit. The reason is that it is on the stack all the time, and a key press can
        get to it each time that it is on top. If only :meth:`result` arms it, there is a
        gap between the rounds. In that gap,
        :meth:`~meshterm.ui.tui.screen.Screen.resolve` has no place to put its value, and the
        key press is lost with no message.
        """
        self._screen.future = asyncio.get_running_loop().create_future()

    async def result(self) -> Any:
        """Await one round of the visited screen: its resolved value, or ``CANCEL`` on Esc.

        A round can be already resolved, because the screen was armed while the caller was
        still busy with the last round. Such a round is returned immediately, and the method
        does not wait for it again.

        Raises:
            PopToMenu: If the user pressed the pop-all key, here or while the caller was
                busy.
        """
        self._session._check_unwind()
        future = self._screen.future
        if future is None:
            self._arm()
            future = self._screen.future
        try:
            return self._session._unpack(await future)
        finally:
            self._arm()  # the next round is live from the moment when this round is read


class TuiSession:
    """A running full-screen TUI: the screen stack, the frame, the input loop, and async prompts."""

    def __init__(
        self,
        header: Callable[[int], RenderableType] | None = None,
        *,
        input: Any = None,  # noqa: A002 - the same name as Application(input=) in prompt_toolkit
        output: Any = None,
    ) -> None:
        """Create a session.

        Args:
            header: A callable that takes the width of the terminal in cells and returns
                the persistent header renderable (the banner and the live status). The
                session calls it again at each paint. With the width, the header can
                stretch the content at its end (the activity sparkline) to fill the row
                exactly. The default is a plain title.
            input: An optional prompt_toolkit input that drives the app (the tests use a
                pipe). The default is the real terminal.
            output: An optional prompt_toolkit output to render to (the tests use a dummy).
                The default is the real terminal.
        """
        self._stack: list[Screen] = []
        self._header = header or (lambda cols: Text("MeshTerm", style="brand"))
        self._app: Application | None = None
        self._input = input
        self._output = output
        # The top floating "working" overlay (a skeleton card), or None when the app is idle.
        # It is not on the screen stack, on purpose: it floats above each layer, and
        # busy_overlay shows and hides it, independently of the screens that are pushed.
        self._overlay: BusyOverlay | None = None
        # The busy card that :meth:`busy_dialog` pushed, or None. Different from the
        # overlay, this card is a screen. It must be a screen, so that it takes the key
        # presses. If it does not take them, the hub below it keeps them for later. This
        # attribute holds the card, so that a nested wait uses the same card instead of a
        # second card on the stack.
        self._busy: BusyDialog | None = None
        # The last composition of each drawn layer: ``layer -> (text, carries a wide
        # glyph)``. With it, :meth:`_emit` can tell a frame that changed from a frame that
        # the 1 Hz paint timer rendered again with no change. It can also find if anything
        # on the screen now must have a full paint. At each paint, this dict is reconciled
        # against the layers that were drawn (refer to :meth:`_reconcile_layers`).
        self._layers: dict[str, tuple[str, bool]] = {}
        # The pop-all key (^W) arms this flag, and the menu loop disarms it after it catches
        # the unwind. While the flag is armed, each navigation boundary raises PopToMenu
        # instead of showing another screen. Refer to :meth:`request_pop_all`.
        self._unwinding = False
        # The screen at the end of the unwind (the main menu). With it, ^W can do nothing
        # when that screen is already the top, instead of a rebuild with no purpose. A
        # dialog also floats over it when no other screen is pushed (refer to
        # :meth:`_floated`). ``None`` until the menu declares it.
        self._root: Screen | None = None
        # The quit confirm that ^Q opens from any screen (refer to :meth:`request_quit`).
        # The app declares it with :meth:`set_quit_confirm`. Then a flag that tells if the
        # confirm is open now. When it is open, the next ^Q quits the app.
        self._quit_confirm: Callable[[], Awaitable[bool]] | None = None
        self._quit_asking = False
        # What the app does to stop the start of new work before it possibly must quit
        # (refer to :meth:`set_quiesce`). Then the quiesce while it is held. Then the
        # one-way flag that :meth:`leave` raises, which tells if the app is on its way out.
        self._quiesce: Callable[[], AbstractAsyncContextManager[Any]] | None = None
        self._quiet: Quiet | None = None
        self._leaving = False
        # The Esc of a terminal, held back while the keyboard says that the key is still
        # down (where something can tell this, refer to :meth:`set_esc_probe`).
        self._watched_esc: WatchedEsc | None = None

    # --- stack ---------------------------------------------------------------

    @property
    def top(self) -> Screen | None:
        """The active screen (the top screen), or ``None`` when the stack is empty."""
        return self._stack[-1] if self._stack else None

    def push(self, screen: Screen) -> None:
        """Push a screen onto the stack, and paint the frame again."""
        self._stack.append(screen)
        self.invalidate()

    def pop(self, screen: Screen | None = None) -> None:
        """Pop ``screen`` (or the top screen) off the stack, and paint the frame again."""
        if not self._stack:
            return
        popped: Screen | None = None
        if screen is None or self._stack[-1] is screen:
            popped = self._stack.pop()
        elif screen in self._stack:
            self._stack.remove(screen)
            popped = screen
        self._expose_overlay()
        # A floating dialog is drawn as a box, sized to its content, over the screen below
        # it. A cell of the dialog can hold a glyph that the terminal drew wider than
        # prompt_toolkit tracks (an emoji or a box-drawing fallback that the differential
        # renderer cannot see). Then the extra part stays as stray characters after the
        # dialog closes. Thus, when the dialog closes, force the frame below it to write
        # each cell again, so that nothing of the dialog stays.
        if popped is not None and getattr(popped, "floating", False):
            self._invalidate_last_frame()
        self.invalidate()

    def reset(self) -> None:
        """Clear the full screen stack, and paint the frame again.

        MeshTerm uses this method to unwind to a blank frame after a cancelled activity. For
        example, when the device disconnects during a session, MeshTerm abandons all the
        screens that the interrupted work pushed. Then it shows the reconnect dialog over a
        clean frame. Screens hold no external resources (their callers pop them in
        ``finally``). Thus it is safe to remove the remaining screens here.
        """
        self._stack.clear()
        # A reset is an unwind itself. The caller has already abandoned all that was running
        # (refer to :func:`~meshterm.ui.menu._session_loop`). Thus an armed ^W has nothing
        # more to unwind. If it stays armed, it goes off in the reconnect flow that
        # replaces the abandoned work.
        self._unwinding = False
        self._expose_overlay()
        self.invalidate()

    def _expose_overlay(self) -> None:
        """Restart the fade of the busy overlay when a stack change shows it again.

        A prompt that is pushed over an active overlay hides the card. A pop back to an empty
        stack shows the card again. Then restart the intro, so that the black hold and the
        fade-in start again from the beginning each time that the card is shown. Without the
        restart, the card comes back immediately at full brightness (refer to
        :meth:`~meshterm.ui.tui.overlay.BusyOverlay.restart`).
        """
        if self._overlay is not None and not self._stack:
            self._overlay.restart()

    # --- navigation ----------------------------------------------------------

    def set_root(self, screen: Screen | None) -> None:
        """Declare ``screen`` as the navigation root: the screen at the end of an unwind.

        The main menu calls this method with its own list. Only two things depend on it:

        - ^W does nothing while the root is already the top screen (you cannot go back to
          where you are).
        - A dialog with nothing below it floats over the root, not over an empty frame
          (:meth:`_floated`).

        No other part of the app must know which screen is the menu.

        Args:
            screen: The root screen, or ``None`` to forget it.
        """
        self._root = screen

    @property
    def root(self) -> Screen | None:
        """The declared navigation root (the main menu), or ``None`` before it is declared."""
        return self._root

    def set_quit_confirm(self, ask: Callable[[], Awaitable[bool]] | None) -> None:
        """Declare the question that the quit chord asks before the app quits.

        The app declares it one time, because only the app knows what the question offers.
        Over a Bluetooth bond, the confirm of the main menu adds *Unpair & quit*, and that
        choice must have the application context, which the session never holds. ``ask``
        floats its dialog and returns whether to quit. It owns all side effects of the answer
        (for example, it stores the record of an unpair).

        Args:
            ask: The confirm, or ``None`` if the quit chord must quit without a question.
        """
        self._quit_confirm = ask

    async def confirm_quit(self) -> bool:
        """Ask the declared quit confirm, as the Quit row of the main menu does.

        It is the same question that ^Q asks, and the same dialog. While the dialog is open,
        the next press of the chord quits immediately (refer to :meth:`request_quit`),
        whichever way opened the dialog.

        Returns:
            Whether the user chose to quit. ``True`` if no confirm is declared.
        """
        self._quit_asking = True
        return await self._asking_quit()

    async def _asking_quit(self) -> bool:
        """Run the declared confirm with :attr:`_quit_asking` already raised, then lower it."""
        try:
            return self._quit_confirm is None or bool(await self._quit_confirm())
        finally:
            self._quit_asking = False

    def request_quit(self) -> None:
        """Answer the quit chord (^Q, and also ^C): ask first, and quit on the second press.

        ^Q is one key from ^W, the chord that goes back to the menu. Thus a wrong key press
        on the way to the menu once closed the app, and with it a running capture or a
        courier queue. Now the chord floats the quit confirm over the current screen, from
        any screen (issue #22, option D). The confirm is a detached flow
        (:meth:`run_detached`). Nothing below it is interrupted, a capture continues behind
        the box, and Esc puts the user back exactly where they were.

        While the confirm is open, the chord quits immediately. Thus the fastest way out is
        still two key presses, and it still works when a screen is stuck or a device read
        does not return. The second press never waits for the dialog to paint or to take a
        key. The flag is raised here, synchronously. Thus two presses that arrive in one
        input batch are still a quit, not two dialogs.

        In the two cases, the app quits cleanly. :meth:`run` cancels the coroutine that
        drives the app and waits for its unwind. Thus the history runs and the chat runs
        close, the services stop, and the exit watchdog is armed, the same as a quit from
        the menu.
        """
        if self._quit_asking or self._quit_confirm is None:
            self._exit_app()
            return
        self._quit_asking = True

        async def ask() -> None:
            if await self._asking_quit():
                self._exit_app()

        self.run_detached(ask())

    def _exit_app(self) -> None:
        """Exit the running application, whatever flow drives it."""
        if self._app is not None and self._app.is_running:
            self._app.exit()

    def set_quiesce(self, enter: Callable[[], AbstractAsyncContextManager[Any]] | None) -> None:
        """Declare how the app stops the start of new work, for when it possibly must quit soon.

        The session enters the quiesce when a held Esc opens its box, and ends it again if
        the user releases the key (refer to :mod:`~meshterm.services.hold_to_quit`).
        :meth:`leave` also enters it on the way out, and then keeps it until the process
        ends. The quiesce must not take anything apart, because a release of the key must
        undo it. The quiesce of the app holds the transmit lock of the device. Thus nothing
        new goes on the air, and a scoped channel send in progress closes its window first.
        The app declares the quiesce, as it declares the quit confirm, because only the app
        has a device.

        Args:
            enter: Makes the async context manager to hold, or ``None`` for nothing.
        """
        self._quiesce = enter

    def set_esc_probe(self, down: Callable[[], bool] | None) -> None:
        """Declare how to ask whether the Esc key is physically down, for the Esc of a terminal.

        With a probe, an Esc that arrives while its key is still down is held back and
        watched as a hold (:class:`~meshterm.ui.tui.holdquit.WatchedEsc`). This gives the
        hold-to-quit gesture where a terminal sends the keys, and a terminal never reports
        that a key comes up. The app declares the probe, because only the app knows that the
        keys are the keys of this machine. They are not the keys of a front end that reads
        them itself (the emulator), and not the keys of another machine over SSH.

        Args:
            down: Tells whether the Esc key is down now
                (:func:`~meshterm.services.hold_to_quit.esc_probe`), or ``None``.
        """
        self._watched_esc = None if down is None else WatchedEsc(self, down)

    @property
    def leaving(self) -> bool:
        """Whether the app is on its way out (:meth:`leave`). Nothing reverses this state."""
        return self._leaving

    def quiet(self) -> Quiet:
        """Engage the quiesce of the app (:meth:`set_quiesce`), or return the held quiesce."""
        if self._quiet is None:
            self._quiet = Quiet(self._quiesce)
        return self._quiet

    def unquiet(self) -> None:
        """Release the quiesce, except after :meth:`leave`: then the app keeps it to the end."""
        if self._quiet is not None and not self._leaving:
            self._quiet.release()
            self._quiet = None

    def leave(self) -> None:
        """Quit the app now, without a question, when no transmission is in progress.

        The end of a held Esc and the SIGTERM of the launcher call this method. The decision
        is already made, so there is no confirm. It is the same exit as Quit (:meth:`run`
        unwinds the flow, and the app tears down), with one more step first. The quiesce of
        the app is engaged, so that a scoped channel send in progress closes its window
        before the device goes. :data:`QUIESCE_WAIT_S` limits that wait, because on the
        Cardputer Zero, the SIGKILL of the launcher comes three seconds after its SIGTERM,
        whatever the app does.

        The method is idempotent and one-way. A SIGTERM that arrives when a hold has just run
        out is the same exit, not a second exit.
        """
        if self._leaving:
            return
        self._leaving = True
        hold_to_quit.leaving()
        quiet = self.quiet()

        async def go() -> None:
            if not await quiet.engaged(QUIESCE_WAIT_S):
                _log.warning("leaving with a transmission still under way")
            self._exit_app()

        self.run_detached(go())

    def request_pop_all(self) -> bool:
        """Arm the unwind to the navigation root (the ^W key). Return whether it is armed.

        The method refuses in two cases, both on purpose:

        * **A modal layer is on top.** A dialog is a question with no answer yet, and a
          progress screen or a busy screen is work in progress
          (:attr:`~meshterm.ui.tui.screen.Screen.modal`). An unwind past either one
          discards something that the user is in the middle of. Thus ^W does nothing there,
          and the user must first answer or wait.
        * **The root is already the top.** There is no place to go.

        In all other cases, the method sets the unwind flag, and resolves the future of
        **each** screen on the stack with :data:`~meshterm.ui.tui.screen.POP_ALL`. It starts
        at the top and goes down to the root, but it never includes the root. Each caller
        that awaits one of these futures changes the value into
        :class:`~meshterm.ui.tui.screen.PopToMenu`.

        The method arms the full stack, not only the top. This makes ^W a verb of the stack,
        not a verb of the call chain. The code that opens a screen from a key handler cannot
        await that screen, because a handler is sync. Thus the live feed floats the packet
        viewer with ``ensure_future(run_screen(...))``, and the chain that could carry an
        unwind ends in a detached task. When the method sent the sentinel only to the top,
        the unwind stopped there, while the hub below waited on a ``visit.result()`` that
        nothing could resolve. ^W closed the viewer and stopped. Then the flag, which was
        still armed, went off at the next key that the user pressed (JP, 2026-08-31). Each
        screen is on the stack, whether something awaits it or not. Thus the stack is the
        reliable way down.

        The walk stops at a **modal** screen, for the same reason as the refusal above for
        the top of the stack: the method must not discard work in progress from below that
        work. The flag stays armed, so the unwind still goes off at the next navigation
        boundary after that screen. If the method resolves a screen that nothing awaits,
        nothing occurs: nothing reads the value, and
        :meth:`~meshterm.ui.tui.screen.Screen.resolve` does nothing on a screen with no
        future.

        The flag is important independently of the futures. A key press can arrive while no
        screen awaits anything (during a device read, below the busy overlay). Then the next
        navigation boundary raises the unwind instead (refer to :meth:`_check_unwind`).

        Returns:
            ``True`` if the unwind was armed, ``False`` if it was refused.
        """
        top = self.top
        if top is not None and (top.modal or top is self._root):
            return False
        self._unwinding = True
        for screen in reversed(self._stack):
            if screen is self._root:
                break  # the unwind ends at the root, and the root itself is not unwound
            if screen is not top and screen.modal:
                break
            screen.resolve(POP_ALL)
        return True

    def unwound(self) -> None:
        """Disarm the unwind. The menu loop calls this after it catches :class:`PopToMenu`."""
        self._unwinding = False

    def _check_unwind(self) -> None:
        """Raise :class:`PopToMenu` if an unwind is armed, before another screen is shown.

        Each navigation boundary calls this method on the way in, and also reads the result
        on the way out. This makes ^W work during a device read. The key arms the flag while
        no future is armed to carry :data:`POP_ALL`. Then the next screen that the flow tries
        to open does not open, and it unwinds instead.
        """
        if self._unwinding:
            raise PopToMenu()

    @staticmethod
    def _unpack(result: Any) -> Any:
        """Return the result of a screen, and change :data:`POP_ALL` into :class:`PopToMenu`.

        The sentinel exists only to go through an ``asyncio.Future``. No caller in the app
        ever sees it. Thus it is changed at the only boundary that reads a future.
        """
        if result is POP_ALL:
            raise PopToMenu()
        return result

    @asynccontextmanager
    async def stay(self, screen: Screen, *, dialog: bool = False) -> AsyncIterator[Visit]:
        """Keep ``screen`` pushed for a full visit, while the screens above it open and close.

        This method is the counterpart of :meth:`run_screen`. Each screen that *owns a loop*
        must use this shape::

            screen = ContactsScreen(...)
            async with session.stay(screen) as visit:
                while True:
                    chosen = await visit.result()
                    if chosen is CANCEL:
                        return
                    await open_node_detail(ctx, chosen)   # nests above the list

        One push and one pop, and the object stays for the full visit. Thus the highlight,
        the sort, the scroll offset, and any live filter are still there when a screen above
        it closes. No ``default=`` restore is necessary, and such a restore can only recover
        the highlight. The screen never leaves the stack. Thus a dialog that the loop opens
        already has the screen as its backdrop, and a full-frame screen that is pushed above
        it draws over it (refer to :meth:`_base_index`). Before, callers got these two
        results by hand: they popped the same screen and pushed it again.

        Args:
            screen: The screen to keep pushed while the block runs.
            dialog: The visit belongs to a dialog: a stepped dialog that turns its pages
                (:func:`~meshterm.ui.menus.run_wizard`), not a hub. Thus it must draw as a
                box, also when it is the only screen on the stack. A blank base goes below it
                for the visit, exactly as :meth:`run_dialog` does for a one-time dialog.

        Yields:
            A :class:`Visit` whose :meth:`~Visit.result` awaits one round of the screen.
        """
        self._check_unwind()
        async with self._floated() if dialog else nullcontext():
            self.push(screen)
            try:
                yield Visit(self, screen)
            finally:
                self.pop(screen)

    def invalidate(self) -> None:
        """Request a paint if the application is running."""
        if self._app is not None:
            self._app.invalidate()

    def request_full_repaint(self) -> None:
        """Force the next paint to write each cell again, then schedule that paint.

        prompt_toolkit paints differentially: it does not touch the cells that are equal to
        the cells of the last frame. Usually we want this. But then damage on the terminal
        side (a double-width fallback glyph that the diff cannot see) stays until those exact
        cells change. Callers use this method when the user leaves a screen that can leave
        stray characters on the terminal (the map). Thus the screen that is drawn below it
        starts from a clean frame.
        """
        self._invalidate_last_frame()
        self.invalidate()

    def _invalidate_last_frame(self) -> None:
        """Remove the cached last frame of prompt_toolkit, so that the next paint writes all cells.

        This method uses an internal of prompt_toolkit. Thus it fails softly if the attribute
        moves.
        """
        renderer = getattr(self._app, "renderer", None)
        if renderer is not None and hasattr(renderer, "_last_screen"):
            renderer._last_screen = None

    def _scrub_columns(self, start: int, stop: int) -> None:
        """Force prompt_toolkit to paint the terminal columns ``[start, stop)`` at the next diff.

        This method writes a sentinel over those cells in the last frame that pt remembers.
        The sentinel can never be equal to real content. Thus the differential renderer
        thinks that the cells changed, and draws them again. This removes a double-width
        fallback glyph that left stray characters on a static edge (the panel border of the
        map, or the border of a floating dialog). It does this without the flicker of the
        full frame that occurs when the full cached frame is removed. This method uses an
        internal of pt, so it fails softly if the structure moves.
        """
        renderer = getattr(self._app, "renderer", None)
        last = getattr(renderer, "_last_screen", None)
        if last is None:
            return
        try:
            from prompt_toolkit.layout.screen import Char

            buffer = last.data_buffer
            sentinel = Char("￿")  # a non-character, never equal to real cell content
            for x in range(max(0, start), stop):
                for row in list(buffer.keys()):
                    buffer[row][x] = sentinel
        except Exception:  # noqa: BLE001 - a cosmetic scrub must never stop the render
            pass

    def _scrub_right_columns(self, count: int) -> None:
        """Scrub the last ``count`` columns of the terminal: the edge of a full-frame panel."""
        cols, _ = self._size()
        self._scrub_columns(cols - count, cols)

    def _scrub_rows(self, rows: Sequence[int]) -> bool:
        r"""Force prompt_toolkit to write these full terminal rows again at the next diff.

        This method is the row version of :meth:`_scrub_columns`, and the cheap form of the
        wide-glyph paint (:meth:`_emit`). It puts the sentinel in each column of each listed
        row. Thus pt finds that the full row changed, and writes it from column 0 in one
        continuous sequence. That is all that is necessary for a row with a glyph that the
        terminal draws narrower than the cells that pt reserved. The method does not touch
        the rows that did not change, and it erases nothing.

        A scrub by row is correct, because the cursor of pt is relative. A step down one row
        writes ``\\r\\n``, which puts the terminal back to a real column 0, whatever the
        drift on the row above. Thus a row with an incorrect measurement can never move the
        rows below it. The drift is important only in a row, and a row that is written again
        in full never jumps inside itself.

        Args:
            rows: The indices of the terminal rows to mark as changed.

        Returns:
            Whether the scrub was done. ``False`` when pt has no remembered frame to scrub
            (it will paint all the frame again), or when the internals moved.
        """
        renderer = getattr(self._app, "renderer", None)
        last = getattr(renderer, "_last_screen", None)
        if last is None:
            return False
        cols, _ = self._size()
        try:
            from prompt_toolkit.layout.screen import Char

            buffer = last.data_buffer
            sentinel = Char("￿")  # a non-character, never equal to real cell content
            for y in rows:
                row = buffer[y]
                for x in range(cols):
                    row[x] = sentinel
        except Exception:  # noqa: BLE001 - a cosmetic scrub must never stop the render
            return False
        return True

    # --- async prompt helpers ------------------------------------------------

    def run_detached(self, work: Any) -> asyncio.Future:
        """Run ``work`` (a flow that opens a screen) from a key handler, without an await.

        The ``handle`` method of a screen is synchronous. Thus a key that opens something
        over the current screen can only start the flow, as a task. Examples are the packet
        viewer of the live feed, the delivery paths of the chat, and the path flows of the
        trace screen. That task is outside the navigation call chain: nothing awaits it.
        Thus a :class:`~meshterm.ui.tui.screen.PopToMenu` that is raised in the task has no
        place to propagate to. It goes to the exception handler of the event loop as an
        unretrieved error.

        It is safe to absorb the exception here, because the unwind never went this way.
        ^W arms each screen on the stack (refer to :meth:`request_pop_all`). Thus the screen
        that opened this screen takes the unwind out, because its own caller awaits it. This
        catch removes only a copy of an unwind that is already in progress. Also, the
        ``finally`` blocks of the flow still run when the exception goes through them.

        Args:
            work: The coroutine to run, usually a :meth:`run_screen` call.

        Returns:
            The task, for a caller that must cancel it or await it. The callers in the app
            are key handlers, and they ignore it. The screen that they opened is on the
            stack, and all other code finds the screen there.
        """

        async def guarded() -> None:
            try:
                await work
            except PopToMenu:
                pass  # the stack carries the unwind, and this task was never on its way

        return asyncio.ensure_future(guarded())

    async def run_screen(self, screen: Screen) -> Any:
        """Push a screen, await its result, then pop it.

        Args:
            screen: The screen to run.

        Returns:
            The resolved value of the screen, or :data:`~meshterm.ui.tui.screen.CANCEL`.

        Raises:
            PopToMenu: If the user pressed the pop-all key: on this screen, or while the
                caller was busy and had nothing pushed.
        """
        self._check_unwind()
        loop = asyncio.get_running_loop()
        screen.future = loop.create_future()
        self.push(screen)
        try:
            result = await screen.future
        finally:
            self.pop(screen)
        return self._unpack(result)

    async def select(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        filterable: bool = True,
        footer_hint: str | None = None,
        delete_hint: str = "",
        floating: bool = False,
    ) -> Any:
        """Show a select screen. Return the chosen value, or ``None`` if the user cancelled.

        ``prompt`` draws an instruction in the box, above the list. ``filterable`` and
        ``footer_hint`` go to the screen for short, fixed lists (a choice of the yes-or-no
        type) that must have no type-to-filter and a special hint. ``delete_hint`` (with rows
        marked :attr:`~meshterm.ui.tui.select.Choice.deletable`) shows the atom of the Delete
        key while the highlight is on such a row. Then Delete resolves a
        :class:`~meshterm.ui.tui.select.DeleteRequest`, which the caller unwraps.
        ``floating`` is the same as in :meth:`text`: the list is a question on the way into a
        tool (which node to administer), not the page of the tool. Thus it must draw as a
        box, also when it is the only screen on the stack. Refer to :meth:`_floated`.
        """
        kwargs: dict[str, Any] = dict(
            prompt=prompt,
            default=default,
            filterable=filterable,
            delete_hint=delete_hint,
        )
        if footer_hint is not None:
            kwargs["footer_hint"] = footer_hint
        runner = self.run_dialog if floating else self.run_screen
        result = await runner(SelectScreen(title, items, **kwargs))
        return None if result is CANCEL else result

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Any | None = None,
        footnote: str | None = None,
        footer_hint: str = "↑↓ move · Enter select · Esc bye",
        keys: Mapping[str, Any] | None = None,
        key_hint: Callable[[Any], str] | None = None,
        live: Callable[[Callable[[list], None]], Awaitable[None]] | None = None,
        hscroll: bool | None = None,
    ) -> Any:
        """Show a chromeless select splash (a banner above a box sized to its content).

        The splash is like :meth:`select`, but it has no header or footer status bars, and
        it is centred below ``banner``. This is the presentation of the startup device
        picker. Here the verb of Esc is ``bye``, the only farewell of the app. This splash is
        the door, and a user who leaves it has started nothing to quit. Type-to-filter is
        off: the device list is short and fixed, so stray key presses never make it shorter.
        An optional ``footnote`` (for example a copyright notice) is muted, below the box.
        When a row accepts removal (a :attr:`~meshterm.ui.tui.select.Choice.deletable` row),
        a "Del remove" atom is added to the footer. But the atom shows only while the
        highlight is on such a row, so the removal key shows itself exactly where it acts
        (refer to :attr:`~meshterm.ui.tui.select.SelectScreen.footer_hint`).

        ``live`` is work to do while the list is visible: the rescan of the device picker,
        which looks for a companion all the time that the splash is open. It gets one
        function, ``redraw(items)``, which replaces the rows while the user looks at them,
        and paints the frame again. The screen itself stays in this method, because a caller
        that held it could not paint it again (``invalidate`` belongs to the session, not to
        the screen). The work runs as a task for exactly as long as the screen runs, and it
        is cancelled when the screen resolves. Thus nothing continues after the list that it
        drew, and no paint goes onto a dialog that opened over the list later.

        ``hscroll`` overrules what the rows imply. A splash whose rows pin no head block
        still must let ←→ read a long row to its end, and a row that pins nothing cannot ask
        for that itself (refer to :class:`~meshterm.ui.tui.select.SelectScreen`).

        ``keys`` gives bare-key shortcuts to the list. A splash can have them because it does
        not filter, so each letter is free (refer to :class:`SelectScreen`). ``key_hint``
        gives their names on the row where the highlight is. Thus they show themselves
        exactly where they act, as ``Del remove`` does.
        """
        delete_hint = (
            "Del remove" if any(isinstance(it, Choice) and it.deletable for it in items) else ""
        )
        screen = SelectScreen(
            title,
            items,
            default=default,
            footer_hint=footer_hint,
            delete_hint=delete_hint,
            filterable=False,
            keys=keys,
            key_hint=key_hint,
            hscroll=hscroll,
        )
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        # The border of the splash is its only hint line, and it is narrow (47 cells on the
        # PicoCalc). Thus, when the sentence becomes wider than the box, it first removes
        # the scroll atom (the move atom in front of it already names ←→). Then it removes
        # the hide key. Of the two shortcuts, the one that stays is the way back. ⇧H shows
        # only after something is hidden, and that is exactly the state where the user must
        # learn how to undo it. At that time, the user has already found h.
        screen.spare_hint_atoms = ("←→ scroll", "h hide")

        def redraw(new_items: list) -> None:
            """Replace the rows and paint again. The user keeps their place, by value."""
            screen.replace_items(new_items)
            self.invalidate()

        worker = asyncio.ensure_future(live(redraw)) if live else None
        try:
            result = await self.run_screen(screen)
        finally:
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
        return None if result is CANCEL else result

    async def confirm_startup(
        self,
        prompt: str | Text,
        *,
        title: str = "",
        confirm_label: str = "Remove",
        banner: Any | None = None,
        footnote: str | None = None,
        backdrop_items: list | None = None,
        backdrop_default: Any = None,
        backdrop_title: str = "Select a companion device",
        footer_hint: str = "←→ choose · Enter select · Esc cancel",
    ) -> bool:
        """Confirm a destructive splash action with a Cancel/verb dialog (chromeless).

        This is the startup-splash version of :meth:`button_dialog`, with the same
        platform-dialog layout. The safe *Cancel* is on the left, and the committing verb is
        on the right and is the default. Thus Enter commits, and Esc backs out. The theme is
        the theme of data loss (the reserved red prompt and border), because this dialog only
        asks before MeshTerm forgets a remembered device. It is the splash version of a
        ``destructive`` :meth:`button_dialog`.

        When ``backdrop_items`` is given, the device list that they describe is drawn again
        as the chromeless base. The red confirm floats over it as a centred box (a modal
        dialog over the pushed backdrop, as each other dialog does). Thus the removal of a
        device looks like a dialog on top of the picker, not a splash that replaces it. The
        ``backdrop_default`` row is highlighted first, so that the user sees that the confirm
        is about it. Without ``backdrop_items``, the confirm draws as its own chromeless
        splash below ``banner`` (the fallback for a caller with no list to float over).

        Args:
            prompt: The question above the buttons.
            title: The short heading in the border of the dialog.
            confirm_label: The label of the committing button (for example ``"Remove"``).
            banner: The wordmark rows above the box (as on the other startup splashes).
            footnote: The muted line below the box.
            backdrop_items: The rows of the picker to draw again behind the confirm.
                ``None`` gives a standalone chromeless confirm splash instead.
            backdrop_default: The row value to highlight in the backdrop list.
            backdrop_title: The heading of the backdrop list (the title of the picker).
            footer_hint: The key hint of the footer.

        Returns:
            ``True`` only when the user chose the committing button. ``False`` on Cancel or
            Esc.
        """
        dialog = ButtonDialog(
            prompt,
            [("Cancel", False), (confirm_label, True)],
            title=title,
            default=1,
            footer_hint=footer_hint,
            prompt_style="err",
            border_style="err",
        )
        if backdrop_items is None:
            # No list to float over: draw the confirm as its own chromeless splash.
            dialog.chrome = False
            dialog.banner = banner
            dialog.footnote = footnote
            return await self.run_screen(dialog) is True
        # Keep the picker on the screen as the chromeless base, and float the red confirm over
        # it. Thus the removal confirm is on top of the device list that it acts on. The
        # backdrop is a static copy of the same rows. It never takes a key, because the dialog
        # above it owns the input.
        backdrop = SelectScreen(
            backdrop_title,
            backdrop_items,
            default=backdrop_default,
            footer_hint="↑↓ move · Enter select · Esc bye",
            delete_hint="Del remove",
            filterable=False,
        )
        backdrop.chrome = False
        backdrop.banner = banner
        backdrop.footnote = footnote
        self.push(backdrop)
        try:
            return await self.run_screen(dialog) is True
        finally:
            self.pop(backdrop)

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Any | None = None,
        footnote: str | None = None,
        footer_hint: str = "Enter continue",
    ) -> None:
        """Show a chromeless message splash (a banner over a boxed renderable) until it closes."""
        screen = ScrollScreen(renderable, title=title, footer_hint=footer_hint)
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        await self.run_screen(screen)

    async def busy_startup(
        self,
        message: str,
        coro: Any,
        *,
        title: str = "",
        banner: Any | None = None,
        footnote: str | None = None,
        interval: float | None = None,
    ) -> Any:
        """Await ``coro``, and show an animated spinner on the chromeless splash while it runs.

        The method keeps the startup splash on the screen (the same wordmark and box). While
        the awaited task runs, the method replaces the content of the splash with an ASCII
        spinner next to ``message``. Then it returns the result of the task. A background
        timer moves the spinner forward and paints the frame again every ``interval``
        seconds. Before the method returns, it always cancels the timer and pops the splash.

        Args:
            message: The line next to the spinner (for example "Talking to Wio on COM5…").
            coro: The awaitable to run (for example a device smoke test).
            title: An optional panel title for the splash box.
            banner: The wordmark rows above the box (as on the other startup splashes).
            footnote: The muted line below the box.
            interval: The seconds between two animation steps of the spinner. The default is
                the cadence of the platform, and on a slow console that is the purpose.
                There, a paint costs more time than this loop waited between two paints
                before. Thus a hardcoded rate used all the event loop to draw the spinner,
                and the work that the spinner reported on did not get time to run.

        Returns:
            The value that ``coro`` resolves to.
        """
        tick = spinner_interval() if interval is None else interval
        screen = BusyScreen(message, title=title)
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        self.push(screen)

        async def animate() -> None:
            while True:
                await asyncio.sleep(tick)
                screen.tick()
                self.invalidate()

        ticker = asyncio.ensure_future(animate())
        try:
            return await coro
        finally:
            ticker.cancel()
            # Await the cancelled ticker, so that it is never garbage-collected while it is
            # still pending. If it is, the loop shows the error "Task was destroyed but it is
            # pending!", which damages the screen. The spinner is cosmetic, so any problem
            # is ignored.
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a small spinner problem must never stop the startup
                pass
            self.pop(screen)

    async def prompt_pin_startup(
        self,
        device_name: str,
        *,
        error: str = "",
        help_text: str = "",
        banner: Any | None = None,
        footnote: str | None = None,
    ) -> str | None:
        """Ask for the Bluetooth PIN of a companion on the chromeless startup splash.

        It is drawn like :meth:`notify_startup` / :meth:`select_startup`: a box with a
        border, centred below ``banner``, with no status bars. Thus the PIN request looks like
        a part of the same device-selection flow. When the method asks again after a rejected
        code, ``error`` shows in the box.

        Args:
            device_name: The display name of the companion, shown in the prompt.
            error: A message about a rejected PIN, to show (empty at the first request).
            help_text: A muted hint below the field.
            banner: The wordmark rows above the box (as on the other startup splashes).
            footnote: The muted line below the box.

        Returns:
            The entered PIN, or ``None`` if the user pressed Esc to cancel.
        """
        screen = PinDialog(device_name, error=error, help_text=help_text)
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        result = await self.run_screen(screen)
        return None if result is CANCEL else result

    async def prompt_text_startup(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        banner: Any | None = None,
        footnote: str | None = None,
    ) -> str | None:
        """Ask for a line of text on the chromeless startup splash (for example a TCP host:port).

        It is drawn like :meth:`prompt_pin_startup`: a :class:`TextScreen` with a border,
        centred below ``banner``, with no status bars. Thus the entry of a network address
        looks like a part of the same device-selection flow. ``validate`` blocks the
        submission of a bad value, the same as the text prompt in the menu.

        Args:
            title: The short heading in the border of the dialog.
            prompt: The instruction in the box, above the field.
            default: The text that is in the field at the start.
            validate: An optional validator that runs on Enter. If it returns a string, the
                submission is blocked.
            help_text: A muted hint below the field.
            banner: The wordmark rows above the box (as on the other startup splashes).
            footnote: The muted line below the box.

        Returns:
            The entered text, or ``None`` if the user pressed Esc to cancel.
        """
        screen = TextScreen(
            title, prompt=prompt, default=default, validate=validate, help_text=help_text
        )
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        result = await self.run_screen(screen)
        return None if result is CANCEL else result

    async def reorder(self, title: str, labels: list[str]) -> list[int]:
        """Show a reorder screen (arrows move rows). Return the final order of the row indices.

        The Apply action row below the list commits the new order. Back (or Esc) cancels. A
        cancel returns the original (identity) order, so that the caller sees it as "no
        change".
        """
        result = await self.run_screen(ReorderScreen(title, labels))
        return list(range(len(labels))) if result is CANCEL else result

    async def text(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        password: bool = False,
        byte_limit: int | None = None,
        floating: bool = False,
    ) -> str | None:
        """Show a text prompt. Return the string, or ``None`` if the user cancelled.

        ``floating`` makes sure that the prompt draws as a centred dialog, also on an empty
        stack. Use it for a question on the way into a tool (a remote-admin password, a typed
        trace target), not for the page of a tool. The prompt then floats over the menu, as
        :meth:`button_dialog` and :meth:`typed_confirm` do (refer to :meth:`run_dialog`).
        When a screen is already below it, there is no difference.

        ``byte_limit`` puts the shared UTF-8 byte gauge on the field, and blocks a submission
        that is longer than the limit. Use it for a field that goes into a packet with a
        maximum size (refer to :class:`TextScreen`).
        """
        screen = TextScreen(
            title,
            prompt=prompt,
            default=default,
            validate=validate,
            help_text=help_text,
            password=password,
            byte_limit=byte_limit,
        )
        runner = self.run_dialog if floating else self.run_screen
        result = await runner(screen)
        return None if result is CANCEL else result

    async def confirm(self, title: str, *, default: bool = True) -> bool | None:
        """Show a yes/no prompt. Return the bool, or ``None`` if the user cancelled."""
        result = await self.run_screen(ConfirmScreen(title, default=default))
        return None if result is CANCEL else result

    async def button_dialog(
        self,
        prompt: str | Text,
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: dict[str, Any] | None = None,
        footer_hint: str = "←→ choose · Enter select · Esc cancel",
        prompt_style: str = "",
        button_style: str = "selected",
        button_idle_style: str = "muted",
        border_style: str = "accent",
        lane: Sequence[fkeys.FPair | None] | None = None,
    ) -> Any:
        """Show a centred button dialog. Return the chosen value, or ``None`` if cancelled.

        A reusable dialog with a prompt above buttons (refer to
        :class:`~meshterm.ui.tui.prompt.ButtonDialog`). The colours, the prompt, the
        buttons, and the single-key shortcuts are all parameters. Thus a caller can give it
        a theme (for example, red for a destructive action), or connect immediate y/n keys.
        ``keys`` maps a shortcut character to the value that it commits. The prompt can be a
        :class:`Text` that already has styles (refer to the ButtonDialog docs).
        """
        screen = ButtonDialog(
            prompt,
            buttons,
            title=title,
            default=default,
            keys=keys,
            footer_hint=footer_hint,
            prompt_style=prompt_style,
            button_style=button_style,
            button_idle_style=button_idle_style,
            border_style=border_style,
            lane=lane,
        )
        result = await self.run_dialog(screen)
        return None if result is CANCEL else result

    @asynccontextmanager
    async def _floated(self) -> AsyncIterator[None]:
        """Make sure that all that is pushed in the block draws as a box, not as a full frame.

        On an empty stack, a lone floating screen is drawn as the background: the full
        chrome, which fills the terminal, and no dialog (refer to :meth:`_base_index`). Thus
        a base is pushed first, and popped when the block ends. The base is the **root**:
        the main menu. The menu is popped while a tool runs, but it is still the screen where
        the user chose the tool. Thus a question on the way into a tool floats over the menu
        that it came from. A dialog is smaller than the frame, and the area around it must
        show the page behind it, never an empty frame. A blank base is the fallback for a
        session with no declared root. When a background is already there, there is nothing
        to do: the dialog floats over it.
        """
        backdrop: Screen | None = None
        if not self._stack:
            backdrop = self._root or ScrollScreen("", floating=False, footer_hint="")
            self.push(backdrop)
        try:
            yield
        finally:
            if backdrop is not None:
                self.pop(backdrop)

    async def run_dialog(self, screen: Screen) -> Any:
        """Run a floating dialog one time, drawn as a centred box (refer to :meth:`_floated`)."""
        async with self._floated():
            return await self.run_screen(screen)

    async def typed_confirm(self, warning: str, word: str, *, title: str = "Are you sure?") -> bool:
        """Ask the user to type ``word`` before a destructive action. Return whether it was typed.

        The method shows the :class:`~meshterm.ui.tui.prompt.TypedConfirmDialog` with the
        error theme, and changes its result into a plain bool: ``True`` only when the user
        typed the word, ``False`` when the user backed out with Esc.
        """
        result = await self.run_dialog(TypedConfirmDialog(warning, word, title=title))
        return result is True

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
    ) -> str | None:
        """Show a free-text prompt with suggestions. Return the text, or ``None`` if cancelled."""
        result = await self.run_dialog(
            AutocompleteScreen(title, choices, prompt=prompt, default=default, validate=validate)
        )
        return None if result is CANCEL else result

    async def message_dialog(self, message: Text | str, *, title: str = "") -> None:
        """Show a short result in a centred dialog with one OK button.

        This is the light acknowledgement counterpart of :meth:`scroll`. A one-line result
        ("✓ flood advertisement sent") is too small for a full result screen. Thus it floats
        as a small dialog over the screen below it. Enter (OK) or Esc closes it. The border
        takes the strongest tone of the message (refer to :func:`_message_border`). Thus an
        error shows in red, and a success stays in the standard accent. The method calls
        :meth:`button_dialog`, which floats the dialog over a blank base when the stack is
        empty (a tool that runs directly from the menu, which is popped while the tool
        runs).

        Args:
            message: The result to show: a :class:`Text` that already has styles (the note
                markup stays in the dialog), or a plain string.
            title: An optional dialog heading (usually the name of the tool or of the
                action).
        """
        await self.button_dialog(
            message,
            [("OK", "ok")],
            title=title,
            footer_hint="Enter OK",
            border_style=_message_border(message),
        )

    async def scroll(
        self, renderable: RenderableType, *, title: str = "", footer_hint: str = ""
    ) -> None:
        """Show a renderable on a scrollable screen that the user can close (a result screen)."""
        screen = ScrollScreen(
            renderable,
            title=title,
            footer_hint=footer_hint or "↑↓ PgUp/PgDn scroll · Esc close",
        )
        await self.run_screen(screen)

    def progress(self, title: str = "Working") -> TuiProgress:
        """Return a progress context manager that uses a pushed :class:`ProgressScreen`."""
        return TuiProgress(self, title)

    @asynccontextmanager
    async def busy_overlay(
        self,
        message: str = "",
        *,
        title: str = "",
        interval: float | None = None,
    ) -> AsyncIterator[BusyOverlay]:
        """Float a skeleton card on top of all other content while a block runs.

        Put a slow operation that affects the screen in this block. The most useful case is
        a device menu navigation, which can otherwise stay on a blank frame while the
        companion answers::

            async with session.busy_overlay("reading…", title="Nodes"):
                await slow_work()

        The card takes the place of the screen whose data MeshTerm reads: a title, and the
        one-cell working chip next to the caption. While the block runs, a background timer
        moves the chip forward, fades the card in, and paints the frame again. At the exit,
        the card is always cleared and the timer cancelled, also after an error. The card
        fades in from black (refer to :attr:`BusyOverlay.brightness`). Thus a quick
        operation paints only an almost black card, and the card never appears suddenly. It
        is drawn only in the gaps between screens (refer to :meth:`_overlay_visible`). Thus
        it shows the wait, and it does not cover a prompt that the user works in.

        Args:
            message: An optional caption next to the working chip.
            title: An optional heading that names the screen whose data MeshTerm reads.
            interval: The seconds between two animation steps (also the cadence of the fade
                paints). The default is the cadence of the platform. Refer to
                :meth:`busy_startup`, which has the same hazard: the card animates over a
                device read. Thus a rate that the console cannot sustain takes the loop from
                the read that the card covers.

        Yields:
            The live :class:`BusyOverlay`, if the caller must change its caption.
        """
        tick = spinner_interval() if interval is None else interval
        # A nested busy_overlay keeps the outer overlay (the outermost wait owns the screen).
        # Its own body still runs, but it does not install a second card.
        if self._overlay is not None:
            yield self._overlay
            return
        overlay = BusyOverlay(message, title=title)
        self._overlay = overlay
        if self._overlay_visible():
            self.invalidate()  # start the fade-in immediately, before the first tick

        async def animate() -> None:
            while True:
                await asyncio.sleep(tick)
                overlay.tick()
                # Paint again only when the card is on the screen. Thus an overlay that waits
                # behind a live prompt does not cause paints of that prompt with no purpose.
                if self._overlay_visible():
                    self.invalidate()

        ticker = asyncio.ensure_future(animate())
        try:
            yield overlay
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a cosmetic overlay must never break a flow
                pass
            self._overlay = None
            self.invalidate()

    @asynccontextmanager
    async def busy_dialog(
        self,
        message: str = "",
        *,
        title: str = "",
        interval: float | None = None,
    ) -> AsyncIterator[BusyDialog]:
        """Float a modal busy card over the current screen while a block runs.

        This is the counterpart of :meth:`busy_overlay` on the stack. A screen must use it
        when that screen itself starts the slow work::

            async with session.busy_dialog("saving Lakeside…", title="Channels"):
                await device.set_channel(...)

        Two things make it different from the overlay, and for these two reasons the
        overlay could not do this job:

        - It is a real :class:`~meshterm.ui.tui.screen.BusyDialog` that is pushed on the
          stack. Thus it draws over a hub, not only in the gaps between screens. The overlay
          paints only on an empty stack (refer to :meth:`_overlay_visible`), and the stack
          is never empty while the user visits a hub.
        - It is modal, so it **owns the keyboard**. A hub that :meth:`stay` keeps up stays
          armed while the work runs. Without a modal layer, each key that the user presses
          during the wait still gets to the hub. Letters go into its live filter, and the
          list comes back with nothing in it. Esc resolves the hub, and the hub is returned
          at the instant when the work finishes: the screen seems to close by itself a
          moment later. Now these key presses get to this card, and stop there.

        Nesting keeps the outermost card. Thus a batch of writes shows as one wait, and a
        box does not flash for each write. The inner block still runs. The caller can
        change the caption of the card while the work continues (``busy.message = …``).
        This is how a sequence tells which step it is on.

        Different from :meth:`busy_overlay`, this card has no fade-in. The fade makes sure
        that a fast operation shows nothing, and that is a good exchange for a cosmetic
        card. But this card is also the key guard, and a guard that arrives late lets some
        keys through.

        Args:
            message: The caption next to the spinner.
            title: An optional heading that names the feature that does the work.
            interval: The seconds between two animation steps of the spinner. The default is
                the cadence of the platform (refer to :meth:`busy_startup`, which has the
                same hazard: the card animates over a device read, so a rate that the console
                cannot sustain takes the loop from the work that the card reports on).

        Yields:
            The live :class:`~meshterm.ui.tui.screen.BusyDialog`, so that its caption can
            change.
        """
        tick = spinner_interval() if interval is None else interval
        if self._busy is not None:  # a nested wait uses the card that its caller already shows
            yield self._busy
            return
        screen = BusyDialog(message, title=title)
        # A lone floating screen on an empty stack is drawn as the background, full-frame and
        # with borders, not as a box. Thus it gets the same blank backdrop that each other
        # dialog uses (refer to :meth:`run_dialog`).
        backdrop = None
        if not self._stack:
            backdrop = ScrollScreen("", floating=False, footer_hint="")
            self.push(backdrop)
        self._busy = screen
        self.push(screen)

        async def animate() -> None:
            while True:
                await asyncio.sleep(tick)
                screen.tick()
                self.invalidate()

        ticker = asyncio.ensure_future(animate())
        try:
            yield screen
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a small spinner problem must never break a flow
                pass
            self._busy = None
            self.pop(screen)
            if backdrop is not None:
                self.pop(backdrop)

    # --- application lifecycle ------------------------------------------------

    async def run(self, main: Any) -> None:
        """Run ``main`` (the menu loop) in the full-screen application.

        The method starts the prompt_toolkit event loop, and drives ``main`` as a background
        task on that same loop. When ``main`` returns, the method exits the application. If
        ``main`` raises an exception, the method raises it again after the loop unwinds.

        Args:
            main: The coroutine that drives the session (usually the menu loop).
        """
        self._app = self._build_app()
        keyboard = get_platform().modifier_watch
        if keyboard:
            # The Shift watcher changes the labels of the F-key lane live. It reports from
            # its own thread, so go to the app loop for the paint. If the watcher cannot
            # start (no input device, no permission), the lane stays static. Refer to the
            # module doc.
            loop = asyncio.get_running_loop()
            modifier_watch.start(lambda: loop.call_soon_threadsafe(self.invalidate), keyboard)
        box: dict[str, BaseException] = {}
        task: dict[str, asyncio.Future] = {}

        async def driver() -> None:
            try:
                await main
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - raised again after the app unwinds
                box["exc"] = exc
            finally:
                # ``is_running`` instead of ``not is_done``: prompt_toolkit clears its future
                # on the way out. Thus ``is_done`` reads False again after the app has
                # finished, and ``exit()`` on a finished app raises. This ``finally`` now
                # also runs after the app is gone (the quit chords exit the app from below
                # us, and then ``run`` cancels this task). Thus it must tell "still up" from
                # "already down", instead of "not yet finished".
                if self._app is not None and self._app.is_running:
                    self._app.exit()

        def pre_run() -> None:
            task["driver"] = asyncio.ensure_future(driver())

        # A held Esc opens the quit box, where a front end can detect one (the emulator).
        # The SIGTERM of the launcher also quits through here. Refer to
        # services.hold_to_quit. The listener costs nothing where nothing reports that a key
        # goes down.
        watch = EscHoldWatch(self, asyncio.get_running_loop())
        hold_to_quit.listen(watch)
        try:
            # Do not let prompt_toolkit install its own loop exception handler. On a stray
            # error in a background task, that handler prints a traceback and a "Press ENTER
            # to continue..." prompt directly over the full-screen UI. When it is disabled,
            # the default handler of asyncio logs such errors to the ``asyncio`` logger
            # instead. That logger goes to the file log (refer to
            # :func:`meshterm.persistence.logging.configure_logging`), and never touches the
            # screen. Errors from ``main`` still propagate through ``driver``/``box``.
            await self._app.run_async(pre_run=pre_run, set_exception_handler=False)
        finally:
            # Whichever way the app exited, it tears down from here. A SIGTERM that arrives
            # now must join this teardown, not interrupt it.
            self._leaving = True
            hold_to_quit.leaving()
            hold_to_quit.unlisten(watch)
        # The app can also exit from below the driver. The quit chords (^Q/^C) call
        # ``Application.exit`` from their detached confirm, or directly from the key handler
        # on the second press (refer to request_quit). Thus ``run_async`` returns while
        # ``main`` still waits on the screen that was open. If nothing is done, that
        # coroutine is abandoned during its await, and its ``finally`` never runs. That
        # ``finally`` closes the history runs and the chat runs, stops the background
        # services, and arms the exit watchdog. Cancel the coroutine and wait for the
        # unwind. Thus a quit from any screen tears down exactly as much as a quit from the
        # menu.
        driver_task = task.get("driver")
        if driver_task is not None and not driver_task.done():
            driver_task.cancel()
            try:
                await driver_task
            except asyncio.CancelledError:
                pass
        if "exc" in box:
            raise box["exc"]

    def _build_app(self) -> Application:
        """Build the prompt_toolkit application, its layout, and its key bindings."""
        base_control = ClusterTextControl(self._render_base, focusable=True)
        base_window = Window(base_control, always_hide_cursor=True)
        # One float with a centred box for each dialog layer on the stack, from bottom to
        # top. Each float renders the k-th screen that floats above the background (refer to
        # :meth:`_float_layers`). Thus a dialog that opens over a dialog draws over it, and
        # the two keep their own sizes. The lower dialog is not stretched to fill the frame.
        # A large fixed pool covers all realistic nesting. The ConditionalContainer hides
        # the layers that this frame does not use.
        dialog_floats = [
            Float(
                ConditionalContainer(
                    Window(
                        ClusterTextControl(lambda i=i: self._render_float_layer(i)),
                        always_hide_cursor=True,
                    ),
                    filter=Condition(lambda i=i: len(self._float_layers()) > i),
                )
            )
            for i in range(_MAX_DIALOG_LAYERS)
        ]
        # The busy overlay is the last float, so it draws on top of each dialog float: the
        # top of the z-order. It is a ``Window`` sized to its content (dont_extend_*), with
        # no anchors. Thus the FloatContainer centres only its skeleton card over the screen,
        # and does not blank the screen.
        overlay_window = Window(
            ClusterTextControl(self._render_overlay),
            always_hide_cursor=True,
            dont_extend_width=True,
            dont_extend_height=True,
        )
        root = FloatContainer(
            content=base_window,
            floats=[
                *dialog_floats,
                Float(
                    ConditionalContainer(overlay_window, filter=Condition(self._overlay_visible))
                ),
            ],
        )
        app = Application(
            layout=Layout(root, focused_element=base_window),
            key_bindings=self._key_bindings(),
            full_screen=True,
            mouse_support=False,
            # Keeps the live monitor counter in the header up to date. The value is set for
            # each platform, so that a host where an idle paint is expensive can wait
            # longer between frames.
            refresh_interval=get_platform().tick_s,
            # Paint as soon as the loop is free, instead of a spin of the event loop for up
            # to 10 ms first. The default postpone of prompt_toolkit protects a UI whose own
            # output floods it with invalidations (the case that caused it was a terminal
            # multiplexer). Here an invalidation is a key press or the 2 s header tick. Thus
            # the delay gives no advantage. We measured it as a constant 3 ms on the
            # PicoCalc and 10 ms on the desktop, for each key press.
            max_render_postpone_time=None,
            input=self._input,
            output=self._resolve_output(),
            # None keeps the default of the output. Refer to _color_depth for the reason
            # why the two prompt_toolkit outputs do not agree, and why the function only
            # increases the depth.
            color_depth=_color_depth(),
        )
        if fastrender.enabled():
            # Use the row-diff renderer for plain full-screen frames. It is built with the
            # same arguments that Application gave the standard renderer. Thus all is
            # unchanged (CPR, alternate screen, mouse, cursor shape), except the paint
            # itself.
            app.renderer = fastrender.FastRenderer(
                app._merged_style,
                app.output,
                full_screen=True,
                mouse_support=False,
                cpr_not_supported_callback=app.cpr_not_supported_callback,
                frame_source=self._plain_frame,
            )
        return app

    def _resolve_output(self) -> Any:
        """The output that the app renders to: the real terminal, with pins and possibly wider.

        Only the real terminal is wrapped (``self._output is None``, so prompt_toolkit would
        build its own output). A test that gives its own output keeps the exact size and the
        exact bytes that it set. Thus a headless render stays deterministic. There are two
        wraps. Each one applies where its own gate says so, and either one can be the only
        one:

        * :class:`~meshterm.ui.tui.colsnap.PinnedOutput`, where
          :func:`~meshterm.ui.tui.colsnap.enabled` is true. Each glyph that the renderer of
          prompt_toolkit writes goes to the column that the renderer measured for it.
        * :class:`_WidthExtendedOutput`, where :func:`_reclaim_last_column` is true (or, if
          the terminal decides, where :func:`_probe_hides_last_column` is true). This wrap is
          the outermost, because the two renderers and the compositor lay out against the
          size that it reports.

        With neither wrap, this is ``None``, and prompt_toolkit builds its own output as
        before.
        """
        pin = colsnap.enabled()
        reclaim = _reclaim_last_column()
        if self._output is not None or not (pin or reclaim is not False):
            return self._output
        from prompt_toolkit.output.defaults import create_output

        output: Any = create_output()
        if reclaim is None:
            reclaim = _probe_hides_last_column(output)
        if not (pin or reclaim):
            return None
        if pin:
            output = colsnap.PinnedOutput(output)
        if reclaim:
            output = _WidthExtendedOutput(output)
        return output

    # --- rendering -----------------------------------------------------------

    def _size(self) -> tuple[int, int]:
        """Return the current (cols, rows) of the terminal."""
        size = self._app.output.get_size()  # type: ignore[union-attr]
        return max(20, size.columns), max(6, size.rows)

    def base_body_size(self) -> tuple[int, int]:
        """Return the ``(width, height)`` in cells that the body of the base screen can use.

        This method copies the layout arithmetic of
        :func:`~meshterm.ui.tui.frame.compose_base`. Thus a full-screen screen (for example
        the map) can size its own content to fill the frame exactly, and it does not wait for
        a paint to learn its height. It copies the two branches of that function. A
        borderless platform replaces the two border rows and the four padding columns of the
        panel with one title-bar row. If a screen uses the arithmetic for borders there, one
        row of the frame that it gets stays blank permanently. The width is the width of
        the current base screen. A :attr:`~meshterm.ui.tui.screen.Screen.flush` screen adds
        the two padding columns that it does not use.

        Returns:
            The width of the inner content and the height of the body viewport, both in
            cells.
        """
        cols, rows = self._size()
        header_h = len(frame.header_lines(self._header(cols), cols))
        if get_platform().frame_border:
            base = self._base_screen()
            inset = frame.panel_inset(base is not None and base.flush)
            # minus footer(1) and panel border(2)
            return cols - inset, max(1, rows - header_h - 1 - 2)
        return cols, max(1, rows - header_h - 1 - 1)  # minus footer(1) and title bar(1)

    def _base_index(self) -> int:
        """The stack index of the full-frame background screen.

        The background is the top screen that does not float: a menu, the map, or a list
        that fills the frame. Each floating dialog above it is drawn as a centred box over
        it (refer to :meth:`_float_layers`). When all the screens on the stack float (a tool
        whose own main screen is a floating select, for example Channels), the bottom screen
        is the background: the dialogs above it must float over it.
        """
        for i in range(len(self._stack) - 1, -1, -1):
            if not getattr(self._stack[i], "floating", False):
                return i
        return 0

    def _base_screen(self) -> Screen | None:
        """The screen drawn as the full-frame background, or ``None`` when the stack is empty."""
        if not self._stack:
            return None
        return self._stack[self._base_index()]

    def _float_layers(self) -> list[Screen]:
        """The floating dialogs on the stack above the background, from bottom to top.

        Each one is drawn as its own centred box over the dialogs below it. Thus when a
        dialog opens over a dialog, the lower dialog keeps its own size, and is not stretched
        to fill the frame (this was the old fault of the compositor with one float).
        """
        if not self._stack:
            return []
        return self._stack[self._base_index() + 1 :]

    def _has_float(self) -> bool:
        """Whether a dialog floats over the background in this frame."""
        return bool(self._float_layers())

    def _emit(self, text: str, layer: str = "base") -> ANSI:
        r"""Wrap a composed frame as a prompt_toolkit :class:`ANSI`.

        A row with a glyph that the terminal may draw narrower than the cells that pt
        reserves for it gets a full paint, not a differential paint.

        prompt_toolkit paints differentially. It writes again only the cells that changed
        since the last frame, and it moves the cursor relative to its own width model. That
        is correct only while each glyph is one cell wide. The terminal can draw a width-2
        glyph in one cell (an emoji in a chat line, a menu icon). Then all the text to its
        right on that row is one column to the left of where pt thinks it is. A later paint
        that jumps into the row (and skips the unchanged emoji) writes at the column of pt,
        one column after the content that it must write over. Thus the old cell stays.

        The repair has a narrow requirement: a row that has such a glyph must be written
        again in full, from column 0, so that the cursor of the terminal lays it out again.
        It is not necessary to erase the screen, or to touch the rows around it. pt goes down
        a row with ``\\r\\n``, which puts the terminal back to a real column 0, whatever the
        drift above it. Thus the incorrect alignment can never spread past its row.

        Thus the changed rows are written again, and nothing else is (:meth:`_scrub_rows`).
        Three rules limit the work to that:

        * **Nothing changed → nothing to do.** The app paints on a 1 Hz timer to move the
          pulse of the header, and an idle screen composes the same each time. pt writes no
          cells, so its cursor cannot drift. The last paint already left the terminal
          aligned. (This case alone caused the flicker: once each second, the app erased a
          screen with an emoji and wrote it again, with no change.)
        * **No wide glyph drawn → nothing to do.** The plain differential paint is exact.
        * **In all other cases, only the rows that changed.** The background composes one
          line for each terminal row, so its diff maps directly onto rows. Thus a header that
          ticks paints the header again, not the screen below it. A float is a centred box,
          and the layout places its rows, not us. Thus when a dialog changes (or closes,
          refer to :meth:`_reconcile_layers`), the fallback still removes the cached frame of
          pt. The same occurs for a frame whose height changed. The user causes these cases,
          and they are not frequent. The timer is neither of the two.
        """
        entry = (text, _has_wide_glyph(text))
        previous = self._layers.get(layer)
        if previous == entry:
            return ANSI(text)
        self._layers[layer] = entry
        if any(wide for _text, wide in self._layers.values()):
            rows = _changed_rows(previous[0], text) if previous is not None else None
            # Line i of the background is terminal row i. No other layer can claim that.
            if layer != "base" or rows is None or not self._scrub_rows(rows):
                self._invalidate_last_frame()
        return ANSI(text)

    def _repaint_if_wide(self) -> None:
        """Change this paint into a full paint if a drawn layer has a wide glyph."""
        if any(wide for _text, wide in self._layers.values()):
            self._invalidate_last_frame()

    def _reconcile_layers(self) -> None:
        """Forget the layers that this paint will not draw, and count their removal as a change.

        A float is hidden when its ``Window`` is removed from the layout. Thus a dialog that
        closes only stops its calls to :meth:`_emit`. Without this method, nothing sees that
        the dialog is gone. Then the cells that it gives back to the base are written again
        in small parts, over a row that the terminal may draw out of position.
        The base layer calls this method at the start of the paint, because the background
        is composed before the floats above it.
        """
        live = {f"float{i}" for i in range(len(self._float_layers()))}
        if self._base_screen() is not None:
            live.add("base")
        if self._overlay_visible():
            live.add("overlay")
        gone = [layer for layer in self._layers if layer not in live]
        for layer in gone:
            self._layers.pop(layer)
        if gone:
            self._repaint_if_wide()

    def _render_base(self) -> ANSI:
        """Render the background screen with the persistent header and footer around it."""
        self._reconcile_layers()  # the paint starts here: the background composes first
        cols, rows = self._size()
        base = self._base_screen()
        if base is None:
            return ANSI("")
        scrub = base.consume_edge_scrub()
        if scrub:
            self._scrub_right_columns(scrub)
        # A bare base (the QR code of the share screen) is only its body on blank rows.
        if base.bare:
            return self._emit(frame.compose_bare(base, cols, rows))
        # A chromeless base (the startup splash) has no header or footer bars. It is
        # centred below its banner, instead of stretched across the terminal.
        if not base.chrome:
            return self._emit(frame.compose_startup(base, cols, rows))
        footer = self.top.footer_hint if self.top else base.footer_hint
        lane = self._fkey_lane(self.top or base)
        return self._emit(
            frame.compose_base(self._header(cols), base, footer, cols, rows, footer_lane=lane)
        )

    def _plain_frame(self) -> str | None:
        """The full frame as rows, when the fast path can do this paint.

        The frame is the background screen, with each floating dialog composited over it
        exactly where the ``FloatContainer`` of prompt_toolkit would centre it (refer to
        :func:`~meshterm.ui.tui.frame.composite_float`). Thus a confirm, a picker, or the
        packet viewer stays on the row-diff path, and does not pay for the full grid rebuild
        of prompt_toolkit at each key press.

        The method returns ``None`` (that is, "let prompt_toolkit lay this one out") for the
        two frames that we do not place ourselves: the busy overlay, which is a ``Window``
        sized to its content that the float container measures, and an empty stack, which
        has no background.

        Each layer still goes through its usual renderer (:meth:`_render_base`,
        :meth:`_render_float_layer`). Thus the layer records that those renderers keep are
        the same, whichever path the paint takes.
        """
        if self._overlay_visible() or not self._stack:
            return None
        rows_out = self._render_base().value.split("\n")
        layers = self._float_layers()
        if layers:
            cols, rows = self._size()
            for index in range(len(layers)):
                rows_out = frame.composite_float(
                    rows_out, self._render_float_layer(index).value, cols, rows
                )
        return "\n".join(rows_out)

    @staticmethod
    def _fkey_lane(active: Screen) -> Callable[[], Text] | None:
        """A deferred F-key lane row for ``active``, or ``None`` on a platform without a lane.

        The row is deferred because the frame resolves it after the body renders. A lane
        dims the slots whose action does nothing, and that information comes from the
        scroll metrics that this paint will store (refer to
        :func:`~meshterm.ui.tui.fkeys.default_lane`).
        """
        deck = fkeys.active_deck()
        if deck is None:
            return None
        return lambda: deck.lane_text(active.fkey_lane, shifted=modifier_watch.shift_down())

    def _render_float_layer(self, index: int) -> ANSI:
        """Render the ``index``-th floating dialog (from bottom to top) as a centred box."""
        layers = self._float_layers()
        if index >= len(layers):
            return ANSI("")
        cols, rows = self._size()
        return self._emit(frame.compose_dialog(layers[index], cols, rows), f"float{index}")

    def _overlay_visible(self) -> bool:
        """Whether to paint the busy overlay in this frame.

        The overlay shows only on an empty screen stack. Thus the card appears only in the
        "black screen" gaps that a device operation opens between screens (for example, a
        menu navigation before the first prompt of the tool). It never covers a dialog that
        the user must read. It also stays unpainted during the initial hold of the overlay
        (:attr:`BusyOverlay.brightness` is 0). Thus an operation that finishes during the
        hold shows nothing and never flashes.
        """
        return self._overlay is not None and not self._stack and self._overlay.brightness > 0

    def _render_overlay(self) -> ANSI:
        """Render the skeleton card of the busy overlay (only when :meth:`_overlay_visible`)."""
        if self._overlay is None:
            return ANSI("")
        return self._emit(self._overlay.render(), "overlay")

    # --- input ---------------------------------------------------------------

    def _key_bindings(self) -> KeyBindings:
        """Build the global key bindings that send normalized actions to the top screen.

        Each key (navigation, Ctrl chord, typed character, pasted text) goes through
        :meth:`_dispatch`. Thus the right-Ctrl rescue there covers the full app, not only the
        screen actions.
        """
        kb = KeyBindings()

        def bind(key: Any, action: str) -> None:
            @kb.add(key)
            def _(event: Any, action: str = action) -> None:  # noqa: ANN401
                self._dispatch(action)

        for key, action in _KEY_ACTIONS.items():
            bind(key, action)

        @kb.add(Keys.Any)
        def _typed(event: Any) -> None:  # noqa: ANN401
            data = event.data
            if not data:
                return
            if len(data) == 1 and data.isprintable():
                self._dispatch("text", data)
            elif len(data) > 1:
                # A bracketed paste (Ctrl-V, right-click, Ctrl-Shift-V) arrives as one
                # multi-character sequence. Give all of it to the top screen as a paste that
                # the screen can confirm and insert. The old len==1 guard removed it instead.
                self._dispatch("paste", data)

        return kb

    def _dispatch(self, action: str, data: str = "") -> None:
        """Send an action to the top screen, and paint the frame again.

        In all the app, the right Ctrl key is first read as Ctrl. While the right Ctrl key is
        physically held, a plain navigation key, or any bare letter that arrives as text,
        changes into its Ctrl chord (refer to :func:`_right_ctrl_down`, :data:`_CTRL_CHORDS`,
        :data:`_CTRL_LETTER_CHORDS`). When the console already reported the chord, this does
        nothing. When a keyboard layout claimed the right Ctrl key and removed it, so that
        only a bare character arrived, this is the rescue. Each binding goes through this
        method. Thus the rescue also covers the chords of the session, not only the screen
        actions: right Ctrl-V pastes into a compose line instead of a typed ``v``, and right
        Ctrl-C quits.

        This method answers the session-level actions itself, and does not send them to a
        screen. No screen ever gets ``to_menu``, ``quit``, ``quit_now``, or
        ``paste_clipboard``.

        The paint keeps the fast differential paint of prompt_toolkit. A frame with a glyph
        that the terminal may draw narrower than the cells that pt reserves for it (an emoji)
        changes itself into a full paint when it is composed. Refer to :meth:`_emit`.
        """
        # An F-key resolves against the lane of the top screen, on the deck of the platform
        # (refer to ui.tui.fkeys), into a normal screen action. With an unassigned slot, a
        # key that the deck does not use, or a platform without a lane, the key press stops
        # here.
        if len(action) in (2, 3) and action[0] == "f" and action[1:].isdigit():
            number = int(action[1:])
            deck = fkeys.active_deck()
            if deck is None:
                return
            if deck.is_shift_key(number):
                # Whatever this resolves to, the keyboard sent this code only because Shift
                # was physically down. Thus latch the shifted labels of the lane, to stop the
                # flicker when Shift is released and pressed again (refer to modifier_watch).
                modifier_watch.note_shift_bank_key()
            top = self.top or self._base_screen()
            resolved = deck.action_for(top.fkey_lane, number) if top else None
            if resolved is None:
                return
            action = resolved
        # The key-state probe comes last in each test. Thus it runs only for a key that can
        # be a chord, not at each key press.
        if action in _CTRL_CHORDS and _right_ctrl_down():
            action = _CTRL_CHORDS[action]
        elif action == "text" and data.lower() in _CTRL_LETTER_CHORDS and _right_ctrl_down():
            action, data = _CTRL_LETTER_CHORDS[data.lower()], ""
        # An Esc of a terminal that is still held down is a hold, not yet a press. Any other
        # key goes after an Esc that is held back, so that the keys keep their order (refer
        # to set_esc_probe).
        watched = self._watched_esc
        if watched is not None:
            if action == "escape":
                if watched.take():
                    self.invalidate()
                    return
            else:
                watched.ahead()
        if action == "to_menu":
            # Pop all the screens on the stack at one time (^W). The unwind is refused over a
            # dialog or over work in progress (refer to request_pop_all). The paint below is
            # still wanted in the two cases. After a refusal, it changes nothing, and an
            # armed unwind will soon change everything.
            self.request_pop_all()
            self.invalidate()
            return
        if action == "quit":
            # Ask first, from any screen. A second press while the question is open quits
            # (refer to request_quit). "Unpair & quit" comes with the confirm that the menu
            # declared.
            self.request_quit()
            self.invalidate()
            return
        if action == "quit_now":
            # Quit immediately, with no question: the ``Quit!`` chip of the main menu, on the
            # Shift half of the ``Quit?`` chip. It is a two-key press on purpose, on the only
            # screen that offers it. It is never a chord that a wrong key press on the way to
            # ^W can hit.
            self._exit_app()
            return
        if action == "paste_clipboard":
            # Some terminals send Ctrl-V as the literal control key, with no bracketed-paste
            # sequence, so the event has no text. Read the OS clipboard here, and give the
            # text to the top screen as a paste. Terminals that change Ctrl-V into a
            # bracketed paste never get here: that paste goes to _typed as a multi-character
            # sequence.
            text = _read_clipboard()
            if text:
                self._dispatch("paste", text)
            return
        top = self.top
        # The edge scroll and its snap-back get the key first (Screen.edge_scroll). A key
        # that acts on a highlight that is not visible first brings the highlight back, and
        # does nothing more. Home/End take the page to its ends. An arrow continues to the
        # screen. If the arrow moved nothing, the next paint scrolls the page instead
        # (Screen.note_highlight).
        if top is not None and not top.edge_scroll(action):
            top.handle(action, data)
        self.invalidate()


__all__ = [
    "TuiSession",
    "Choice",
    "Separator",
    "SelectScreen",
    "ReorderScreen",
    "ScrollScreen",
]
