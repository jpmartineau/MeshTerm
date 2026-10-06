# SPDX-License-Identifier: Apache-2.0
"""The arithmetic that sets the size and the flags of a trace.

This module has two pure functions and the four constants that they use. The first
function encodes the path-hash width of each hop into the ``flags`` byte of the trace. The
second function finds how long to wait for the reply of a trace, from the route that the
trace must go through. Both functions are protocol arithmetic, with no device, no storage,
and no rendering. :class:`~meshterm.core.connection.MeshCoreDevice` must have them on the
send path. They are in this module so that ``core`` never has to import them from
``services``. The trace runner (:mod:`meshterm.services.trace_runner`) exports both again
under their own names, and all the other callers still read them from there.
"""

from __future__ import annotations


def path_hash_flags(width_bytes: int) -> int | None:
    """Return the trace ``flags`` value that encodes the path-hash width of each hop.

    The trace subsystem encodes the hash size as ``1 << (flags & 3)``, on the send side
    and on the receive side (``send_trace`` and the code that reads ``TRACE_DATA``). Thus
    the flags can encode only the widths 1, 2, 4, and 8 bytes. This encoding is
    independent of the ``mode = size - 1`` encoding for contact routing
    (``out_path_hash_mode``), and it is different.

    Args:
        width_bytes: The path-hash width in bytes.

    Returns:
        The flags value (the exponent ``s``), or ``None`` if the width is not a power of
        two that the flags can encode.
    """
    for s in range(4):
        if (1 << s) == width_bytes:
            return s
    return None


#: The fixed part of the reply-wait budget of a trace, in seconds. It covers the transmission
#: of our node, the turnaround at the far endpoint, and the decode on our receive side. This
#: cost does not increase with the route.
TRACE_TIMEOUT_BASE_S = 4.0
#: The time that the budget adds for each hop, in seconds. Each entry in the path of the
#: trace is one relay transmission. This time covers the airtime of its packet, the
#: processing in the repeater, and the random transmit backoff that MeshCore adds so that
#: relays do not collide.
TRACE_TIMEOUT_PER_HOP_S = 1.6
#: The maximum reply wait, in seconds. Thus, when no reply comes back over a route, the
#: trace still stops (and releases the session) in a limited time. The wait does not
#: increase without limit.
TRACE_TIMEOUT_CEILING_S = 30.0
#: The budget, in seconds, when the hop count is not known before the send: a flood with no
#: path, to a contact for which MeshTerm has no route. This value is the old flat value. We
#: keep it only for the case where MeshTerm cannot know the length of the route.
TRACE_TIMEOUT_FLOOD_S = 10.0


def trace_timeout(hop_count: int) -> float:
    """Return the reply-wait budget, in seconds, for a trace through ``hop_count`` hops.

    A trace is one packet only. It must go through the full path before its reply gets to
    our node: out to the far hop, and back over the mirrored return leg. Thus the wait must
    increase with the route. A flat budget for a neighbour stops a long trace before it is
    complete. The packet completes the circuit on the mesh, but our wait has already
    ended. Thus MeshTerm logs a false "no reply" for a route that worked. The budget is a
    fixed base (:data:`TRACE_TIMEOUT_BASE_S`), plus a time for each relay transmission
    (:data:`TRACE_TIMEOUT_PER_HOP_S`). Its maximum is :data:`TRACE_TIMEOUT_CEILING_S`.

    ``hop_count`` is the number of hops in the transmitted path. That path already has the
    mirrored return leg (refer to
    :meth:`~meshterm.core.connection.MeshCoreDevice._trace_path_to_contact`). Thus each
    hop is one relay transmission, and this function must not double the count.

    Args:
        hop_count: The number of hops in the path of the trace, with the return leg. ``0``
            (or less) means that the count is not known (a flood with no path). Then the
            result is :data:`TRACE_TIMEOUT_FLOOD_S`.

    Returns:
        The number of seconds to wait for the trace reply.
    """
    if hop_count <= 0:
        return TRACE_TIMEOUT_FLOOD_S
    budget = TRACE_TIMEOUT_BASE_S + TRACE_TIMEOUT_PER_HOP_S * hop_count
    return min(budget, TRACE_TIMEOUT_CEILING_S)
