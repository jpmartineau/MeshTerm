# SPDX-License-Identifier: Apache-2.0
"""A small, central, reusable library for a full-screen text UI.

The package uses prompt_toolkit for the terminal, the input, and the resize events, and
Rich to render all the content. It gives the interactive app a UI that is consistent,
bounded, and in layers:

- A screen never becomes larger than the terminal.
- Long content scrolls.
- When the user goes deeper, each dialog is put over its dimmed parent.
- The Esc key always closes the current layer.

Navigation is a strict stack. Each screen that the user enters is one push, and each Esc is
one pop. The screen object stays for the whole visit. Thus its highlight, its sort, and its
filter are still there when a screen above it closes
(:meth:`~meshterm.ui.tui.session.TuiSession.stay`). The only shortcut past the stack is ^W.
It unwinds all the screens on the stack at one time, because it raises
:class:`~meshterm.ui.tui.screen.PopToMenu` through them.

Public API:
    - :class:`~meshterm.ui.tui.session.TuiSession`: the running session and the prompt
      helpers.
    - :class:`~meshterm.ui.tui.select.Choice` / :class:`~meshterm.ui.tui.select.Separator`:
      the items for ``session.select``.
    - The screen classes, for advanced or custom layers.
"""

from __future__ import annotations

from .overlay import BusyOverlay
from .progress import ProgressScreen
from .prompt import (
    CHANNEL_BYTE_LIMIT,
    DM_BYTE_LIMIT,
    AutocompleteScreen,
    ConfirmScreen,
    ReconnectDialog,
    TextScreen,
    TypedConfirmDialog,
    byte_counter,
)
from .screen import CANCEL, BusyDialog, BusyScreen, PopToMenu, Screen, ScrollScreen
from .select import Choice, DeleteRequest, KeyRequest, SelectScreen, Separator
from .session import TuiSession, Visit
from .spinner import Spinner

__all__ = [
    "TuiSession",
    "Screen",
    "ScrollScreen",
    "BusyDialog",
    "BusyScreen",
    "SelectScreen",
    "Choice",
    "DeleteRequest",
    "KeyRequest",
    "Separator",
    "TextScreen",
    "ConfirmScreen",
    "AutocompleteScreen",
    "ReconnectDialog",
    "TypedConfirmDialog",
    "ProgressScreen",
    "Spinner",
    "BusyOverlay",
    "CANCEL",
    "PopToMenu",
    "Visit",
    "DM_BYTE_LIMIT",
    "CHANNEL_BYTE_LIMIT",
    "byte_counter",
]
