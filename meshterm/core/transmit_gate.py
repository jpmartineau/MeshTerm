# SPDX-License-Identifier: Apache-2.0
"""The shared transmit clock: how long ago we last put something on the air.

MeshTerm has always paced its own bursts — a trace waits between samples, a TX sweep
between levels — but each did it privately, inside its own loop. Nothing knew what
*another* feature had just transmitted, so a trace could finish and an advert go out a
tenth of a second later, and no screen could say why it was making you wait.

This is that missing clock. Every real transmission marks it (see the send methods in
:mod:`meshterm.core.connection`, which are the only place the radio is actually spoken
to), and anything about to transmit can ask :func:`remaining` how long is left before it
politely may. The waiting itself is the UI's job — see
:func:`meshterm.ui.cooldown.wait_for_cooldown`, which turns a wait worth noticing into a
countdown you can back out of.

Two clocks, because two kinds of transmission cost the mesh very different amounts:

* **every transmission**, spaced by the ``trace_cooldown_s`` preference. This is duty-cycle
  courtesy: our own airtime, spread out.
* **flood adverts**, spaced by the longer ``flood_advert_cooldown_s``. A flood advert is
  the most expensive thing a node can ask for — every repeater in range rebroadcasts it —
  so it waits out both clocks, and its own is floored well above the general one.

Timing is :func:`time.monotonic`, not the wall clock: this measures an elapsed interval,
and a clock sync mid-session (which MeshTerm can itself trigger) must not make a cooldown
appear to have elapsed, or to have years left.

Like :func:`meshterm.core.preferences.current`, the gate is a process-wide singleton
rather than something passed down: the layer that transmits and the layer that waits are
far apart, and threading a clock between them would touch every call in between for
nothing.
"""

from __future__ import annotations

import time


class TransmitGate:
    """The last-transmission clock, and the wait it implies.

    :meth:`mark` records that something went out; :meth:`remaining` says how long a
    caller should hold off. A fresh gate has transmitted nothing, so nothing is owed.
    """

    def __init__(self) -> None:
        """Start an idle gate — nothing sent, nothing owed."""
        self._last_sent: float | None = None
        self._last_flood_advert: float | None = None

    def mark(self, *, flood_advert: bool = False) -> None:
        """Record a transmission as having just finished.

        Called *after* the send returns, so the cooldown counts from the moment the air
        was free again rather than from when we started talking.

        Args:
            flood_advert: Whether the transmission was a flood advert, which starts its
                own longer clock in addition to the general one.
        """
        now = time.monotonic()
        self._last_sent = now
        if flood_advert:
            self._last_flood_advert = now

    @property
    def last_sent(self) -> float | None:
        """When anything last went out, on :func:`time.monotonic`'s clock; ``None`` if never.

        For a listener that treats our own transmissions as breaking the air's silence
        (the weekly advert's wait for a quiet spell) rather than as a cooldown to wait out.
        """
        return self._last_sent

    def remaining(self, *, flood_advert: bool = False) -> float:
        """Seconds a caller should wait before transmitting; ``0.0`` when it may go now.

        Args:
            flood_advert: Whether the transmission being considered is a flood advert. A
                flood advert waits out *both* clocks — the general cooldown since anything
                was sent, and the flood cooldown since the last flood advert — so this
                returns whichever is longer.

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
        """Forget both clocks, as if nothing had ever been sent.

        For a fresh session and for tests; there is no user-facing way to clear a cooldown,
        which would rather defeat it.
        """
        self._last_sent = None
        self._last_flood_advert = None


def _left(last: float | None, cooldown: float) -> float:
    """Seconds left of ``cooldown`` since ``last``, or ``0.0`` if it never happened."""
    if last is None or cooldown <= 0:
        return 0.0
    return max(0.0, cooldown - (time.monotonic() - last))


#: The clock for this process. A module-level singleton for the reason given in the module
#: docstring: the transmitting layer and the waiting layer never meet.
_gate = TransmitGate()


def current() -> TransmitGate:
    """The transmit clock in force for this process."""
    return _gate


def mark(*, flood_advert: bool = False) -> None:
    """Record a transmission on the process-wide clock (see :meth:`TransmitGate.mark`)."""
    _gate.mark(flood_advert=flood_advert)


def remaining(*, flood_advert: bool = False) -> float:
    """The wait owed on the process-wide clock (see :meth:`TransmitGate.remaining`)."""
    return _gate.remaining(flood_advert=flood_advert)


__all__ = ["TransmitGate", "current", "mark", "remaining"]
