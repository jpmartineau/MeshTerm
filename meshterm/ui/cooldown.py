# SPDX-License-Identifier: Apache-2.0
"""Wait for the end of a transmit cooldown, with a visible countdown that the user can cancel.

MeshTerm waits between two transmissions (refer to :mod:`meshterm.core.transmit_gate`).
A short wait does not need a message. But a long wait with a frozen screen looks like an
app that does not respond. And it occurs on the screen where the first question of the
user is "did the radio die?". Thus the wait has a threshold:

- At or under :data:`SILENT_WAIT_S`, the wait occurs with no message.
- Over it, the wait becomes a :class:`~meshterm.ui.tui.prompt.CountdownDialog`. The
  seconds count down above a Cancel chip, in a dialog that floats on the screen that
  asked. The user can leave the dialog to cancel the action completely.

There is one function, :func:`wait_for_cooldown`. Its result is the only thing that a
caller tests. ``True`` means that the cooldown is over and the action can continue.
``False`` means that the user changed their mind.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..core import transmit_gate

if TYPE_CHECKING:
    from ..context import AppContext

#: A wait of this number of seconds or less occurs with no message. The value is long
#: enough for the usual time between two transmissions. It is also short enough that the
#: user never waits in front of a screen that does not change and does not say why. If
#: a dialog shows for only half a second, that dialog is itself the interruption.
SILENT_WAIT_S = 2.0

#: The time between two paints of the countdown. It is fast enough that the seconds seem
#: to count down smoothly instead of in jumps. It is slow enough that it costs nothing.
_TICK_S = 0.2


async def wait_for_cooldown(ctx: AppContext, *, action: str, flood_advert: bool = False) -> bool:
    """Wait until the transmit cooldown is over. Return ``False`` if the user cancels.

    Args:
        ctx: The shared application context (for the UI surface).
        action: The action that waits, named as the event that will occur next. It
            becomes the title of the dialog. Thus the title is "Flood advert", not
            "Waiting to send a flood advert".
        flood_advert: ``True`` if the transmission that waits is a flood advert. A flood
            advert waits for its own, longer clock, and also for the general clock.

    Returns:
        ``True`` when the wait is over and the caller can transmit. ``False`` if the user
        cancelled the countdown.
    """
    remaining = transmit_gate.remaining(flood_advert=flood_advert)
    if remaining <= 0:
        return True
    if remaining <= SILENT_WAIT_S:
        await asyncio.sleep(remaining)
        return True

    session = getattr(ctx.ui, "session", None)
    if session is None:
        # There is no full-screen session on which a dialog can float (the scripted CLI).
        # The wait is still necessary, thus MeshTerm still waits. It waits with no message,
        # the same as each other pause in the CLI.
        await asyncio.sleep(remaining)
        return True

    from .tui import CANCEL
    from .tui.prompt import CountdownDialog

    reason = (
        "a flood advert reaches the whole mesh"
        if flood_advert
        else "spacing out our own transmissions"
    )
    dialog = CountdownDialog(action, remaining, reason=reason)
    ticker = asyncio.ensure_future(_tick(session, dialog, flood_advert))
    try:
        return await session.run_screen(dialog) is not CANCEL
    finally:
        ticker.cancel()


async def _tick(session, dialog, flood_advert: bool) -> None:  # noqa: ANN001
    """Give the live remaining time to the dialog, until the dialog resolves itself at zero."""
    try:
        while True:
            await asyncio.sleep(_TICK_S)
            dialog.set_remaining(transmit_gate.remaining(flood_advert=flood_advert))
            session.invalidate()
    except asyncio.CancelledError:
        # Catch the cancellation and ignore it, so that the task ends on its own last loop
        # cycle. Thus the loop does not destroy the task while it is pending. The animator
        # of the progress dialog does the same thing.
        pass


__all__ = ["SILENT_WAIT_S", "wait_for_cooldown"]
