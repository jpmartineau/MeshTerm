# SPDX-License-Identifier: Apache-2.0
"""The shared transmit clock: how long ago our node last transmitted.

MeshTerm always paced its own bursts: a trace waits between samples, and a TX sweep waits
between levels. But each feature did it privately, inside its own loop. No feature knew
what another feature transmitted a moment before. Thus a trace could finish and an advert
could go out a tenth of a second later, and no screen could tell why it made you wait.

This module is that missing clock. Each real transmission marks it (refer to the send
methods in :mod:`meshterm.core.connection`, which are the only place that speaks to the
radio). Code that is about to transmit can ask :func:`remaining` how long it must wait to
transmit politely. The wait itself is the job of the UI. Refer to
:func:`meshterm.ui.cooldown.wait_for_cooldown`, which changes a wait that is long enough
to notice into a countdown that you can cancel.

There are two clocks, because two types of transmission cost the mesh very different
amounts:

* **Each transmission**, spaced by the ``trace_cooldown_s`` preference. This is duty-cycle
  courtesy: our own airtime, spread out.
* **Flood adverts**, spaced by the longer ``flood_advert_cooldown_s``. A flood advert is
  the most expensive thing that a node can ask for, because each repeater in range relays
  it. Thus a flood advert waits for both clocks, and the minimum of its own clock is much
  higher than the minimum of the general clock.

The timing uses :func:`time.monotonic`, not the wall clock, because this module measures
an elapsed interval. A clock sync during a session (which MeshTerm itself can start) must
not make a cooldown look finished, or look as if it has years left.

The gate is a singleton for the full process, the same as
:func:`meshterm.core.preferences.current`. Code does not pass it down. The reason: the
layer that transmits and the layer that waits are far apart. To pass a clock down between
them changes each call between them, and gives no benefit.
"""

from __future__ import annotations

import time


class TransmitGate:
    """The clock of the last transmission, and the wait that it causes.

    :meth:`mark` stores that something went out. :meth:`remaining` tells how long a
    caller must wait. A new gate transmitted nothing, so no wait is necessary.
    """

    def __init__(self) -> None:
        """Start an idle gate: nothing sent, and no wait."""
        self._last_sent: float | None = None
        self._last_flood_advert: float | None = None

    def mark(self, *, flood_advert: bool = False) -> None:
        """Store a transmission that finished at this moment.

        MeshTerm calls this method after the send returns. Thus the cooldown counts from
        the moment when the air was free again, not from when our node started to
        transmit.

        Args:
            flood_advert: Whether the transmission was a flood advert. A flood advert
                starts its own longer clock, in addition to the general clock.
        """
        now = time.monotonic()
        self._last_sent = now
        if flood_advert:
            self._last_flood_advert = now

    @property
    def last_sent(self) -> float | None:
        """When something last went out, on the :func:`time.monotonic` clock (or ``None``).

        This is for a listener that reads our own transmissions as an end of the silence
        on the air (the weekly advert waits for a quiet period), not as a cooldown to wait
        for. ``None`` means that nothing ever went out.
        """
        return self._last_sent

    def remaining(self, *, flood_advert: bool = False) -> float:
        """The seconds that a caller must wait before it transmits (``0.0`` to go now).

        Args:
            flood_advert: Whether the planned transmission is a flood advert. A flood
                advert waits for both clocks: the general cooldown since the last
                transmission, and the flood cooldown since the last flood advert. Thus
                the method returns the longer of the two waits.

        Returns:
            The wait in seconds, never negative.
        """
        from .preferences import current as current_preferences

        preferences = current_preferences()
        wait = _left(self._last_sent, preferences.trace_cooldown_s)
        if flood_advert:
            wait = max(wait, _left(self._last_flood_advert, preferences.flood_advert_cooldown_s))
        return wait

    def reset(self) -> None:
        """Forget both clocks, as if nothing was ever sent.

        This is for a new session and for tests. The user has no way to clear a cooldown,
        because such a way makes the cooldown useless.
        """
        self._last_sent = None
        self._last_flood_advert = None


def _left(last: float | None, cooldown: float) -> float:
    """The seconds of ``cooldown`` that remain after ``last``, or ``0.0`` if it never occurred."""
    if last is None or cooldown <= 0:
        return 0.0
    return max(0.0, cooldown - (time.monotonic() - last))


#: The clock for this process. It is a module-level singleton, for the reason that the
#: module docstring gives: the layer that transmits and the layer that waits never meet.
_gate = TransmitGate()


def current() -> TransmitGate:
    """The transmit clock that applies to this process."""
    return _gate


def mark(*, flood_advert: bool = False) -> None:
    """Store a transmission on the clock of the process (refer to :meth:`TransmitGate.mark`)."""
    _gate.mark(flood_advert=flood_advert)


def remaining(*, flood_advert: bool = False) -> float:
    """The necessary wait on the process clock (refer to :meth:`TransmitGate.remaining`)."""
    return _gate.remaining(flood_advert=flood_advert)


__all__ = ["TransmitGate", "current", "mark", "remaining"]
