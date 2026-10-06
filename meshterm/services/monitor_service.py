# SPDX-License-Identifier: Apache-2.0
"""The history logger of the passive monitor.

Passive monitoring stores each advert or telemetry packet that the companion hears in the
database. Thus it builds the long-term history that the coverage map and the link-quality
alerts read. It does not listen itself: the
:class:`~meshterm.services.event_hub.EventHub` (``ctx.events``), which is always on, does
the listening. This service is one of its subscribers: the subscriber that writes
observations to the database.

The recording is always on: there is no switch for the user. The subscription is
registered at the start of the session, and it does not touch the device. Thus
observations arrive as soon as the hub operates, also when the hub opens only when
necessary (``connect_on_start`` off, or no device selected yet).

The service is state for the session on the :class:`~meshterm.context.AppContext`
(``ctx.monitor``). It owns the hub subscription that stores observations, and the counters
that the menu header shows live.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent
from ..core.models import Observation, utcnow

if TYPE_CHECKING:
    from ..context import AppContext

#: The time (seconds) of one bucket of the all-packet activity histogram: one minute. That
#: is one column of braille dots in the header indicator and in the activity chart of the
#: dashboard.
ACTIVITY_BUCKET_S = 60

#: The number of one-minute buckets that the histogram keeps: six hours. This is enough for
#: the header and the dashboard to make their charts as wide as any realistic terminal (two
#: buckets for each cell), and still draw history, not padding.
ACTIVITY_BUCKETS = 360


class MonitorService:
    """Store heard packets in the history, as a subscriber of the event hub (always on).

    The attributes are private. Use the properties and the async lifecycle methods
    (:meth:`start`, :meth:`stop`, :meth:`aclose`).
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize the (idle) service.

        Args:
            ctx: The shared application context (device, repository, logger).
        """
        self._ctx = ctx
        self._unsubscribe: Unsubscribe | None = None  # the hub subscription, while it records
        self._count_unsubscribe: Unsubscribe | None = None  # for the all-packet counter
        self._run_id: int | None = None
        self._session_count = 0
        self._run_start_count = 0
        # The event-loop callback puts observations in a queue for this worker (refer to
        # start()). Thus a burst of heard packets is written to disk between paints, and
        # does not block them. This is the same design as the inbound queue of ChatService.
        self._queue: asyncio.Queue[Observation] | None = None
        self._worker: asyncio.Future | None = None
        # The activity of all packets, in buckets by wall-clock minute (epoch // span →
        # count). Each hub event counts (observations, messages, acks), because the
        # indicator in the header answers "is the mesh alive?", not "any mail?". Old
        # buckets are removed as time goes on. Thus it never holds more than the six-hour
        # window plus one closing bucket.
        self._activity: dict[int, int] = {}
        # The last histogram and flags tuples, cached with the values from which they
        # come: the current minute bucket and (for the histogram) the packet-arrival stamp
        # below. The header builds both again at each paint of each screen. Before this
        # cache, between two packets or two minutes, that was 720 dict lookups in each
        # frame for identical tuples.
        self._activity_stamp = 0
        self._histogram_cache: tuple[int, int, tuple[int, ...]] = (-1, -1, ())
        self._flags_cache: tuple[int, tuple[bool, ...]] = (-1, ())
        # The counts for each packet class (advert/telemetry/packet/message/ack). The
        # stored history below gives the first values. Then the subscription that does not
        # filter by kind adds the live counts. This is the traffic panel of the dashboard,
        # which stays from one session to the next.
        self._kind_counts: dict[str, int] = {}
        # Housekeeping: one time in each session, delete the observations that are older
        # than the retention window. Thus a database that always stores new data keeps a
        # limited size (0 = keep for all time).
        preferences = getattr(ctx, "preferences", None)
        days = (preferences.history_days if preferences is not None else 0) or 0
        if days:
            try:
                pruned = ctx.repo.prune_observations(utcnow() - timedelta(days=days))
                if pruned:
                    ctx.log.info("history housekeeping: pruned %s observations", pruned)
            except Exception as exc:  # noqa: BLE001 - housekeeping must never block startup
                ctx.log.debug("history housekeeping failed: %s", exc)
        # The total number of observations in the database when the session started. The
        # live "total" is this number plus what we capture in this session (this process
        # is the only writer during an interactive session). Thus no DB count is necessary
        # at each paint.
        self._start_total = ctx.repo.observation_count()
        # The minute at which this session started. The buckets at or after it hold live
        # traffic, and the older buckets hold the history from the database. The activity
        # charts use this boundary for their colours.
        self._start_bucket = int(time.time() // ACTIVITY_BUCKET_S)
        self._seed_from_history()

    def _seed_from_history(self) -> None:
        """Fill the activity buckets and the kind counts from the stored observations.

        This gives the dashboard its persistence. A new session opens with data: the
        activity chart already shows the last six hours, and the traffic panel shows its
        counts for all the history. Without this method, the chart is empty, and it fills
        only while the app runs. Then live events add to these values (they are *new* rows,
        thus nothing counts two times). The stored history has only observations. Messages
        and acks start to count from zero in each session.
        """
        try:
            window = timedelta(seconds=ACTIVITY_BUCKET_S * ACTIVITY_BUCKETS)
            for obs in self._ctx.repo.recent_observations(since=utcnow() - window):
                bucket = int(obs.observed_at.timestamp() // ACTIVITY_BUCKET_S)
                self._activity[bucket] = self._activity.get(bucket, 0) + 1
            self._kind_counts = dict(self._ctx.repo.kind_counts())
        except Exception as exc:  # noqa: BLE001 - a failed start is worse than an empty chart
            self._ctx.log.debug("monitor: history seed failed: %s", exc)

    @property
    def active(self) -> bool:
        """True while the service stores observations in the history."""
        return self._unsubscribe is not None

    @property
    def session_count(self) -> int:
        """The number of observations captured after this process started."""
        return self._session_count

    def total_count(self) -> int:
        """Return the total number of observations stored, for all time (with this session)."""
        return self._start_total + self._session_count

    def activity_histogram(self) -> tuple[int, ...]:
        """The counts of all packets for each one-minute bucket over the last six hours.

        The newest bucket is first (index 0 is the current minute). The chart widgets
        expect this order (they draw "now" at the right edge). At the start of the
        session, the stored observations give the first values. Then all the events that
        the hub sends out add live counts. Thus after a restart, the pulse in the header
        and the chart on the dashboard open with data. Each consumer takes the part of the
        window that fits its chart, and keeps the rest as history in reserve.

        Returns:
            :data:`ACTIVITY_BUCKETS` bucket counts.
        """
        bucket = int(time.time() // ACTIVITY_BUCKET_S)
        stamp = self._activity_stamp
        cached_bucket, cached_stamp, histogram = self._histogram_cache
        if bucket != cached_bucket or stamp != cached_stamp:
            histogram = tuple(self._activity.get(bucket - i, 0) for i in range(ACTIVITY_BUCKETS))
            self._histogram_cache = (bucket, stamp, histogram)
        return histogram

    def activity_session_flags(self) -> tuple[bool, ...]:
        """For each histogram bucket, True if the bucket holds traffic of this session.

        The flags align with :meth:`activity_histogram` (newest first). ``True`` for the
        buckets at or after the first minute of the session, and ``False`` for the history
        from the database. The activity charts use this split: they draw live traffic in
        green, and the traffic of an earlier session in grey.

        Returns:
            :data:`ACTIVITY_BUCKETS` flags, newest first.
        """
        bucket = int(time.time() // ACTIVITY_BUCKET_S)
        cached_bucket, flags = self._flags_cache
        if bucket != cached_bucket:
            flags = tuple(bucket - i >= self._start_bucket for i in range(ACTIVITY_BUCKETS))
            self._flags_cache = (bucket, flags)
        return flags

    def kind_counts(self) -> dict[str, int]:
        """The counts for each packet class: the advert/telemetry/packet buckets, message, ack.

        At the start of the session, the stored history gives the first values. After
        that, the live counts add to them. Thus the numbers describe all that the recorder
        keeps, not only this session. The observation classes come from the packet itself
        (``Observation.kind``). A raw ``packet`` frame with a parsed payload class goes in
        the bucket ``packet:<TYPENAME>``, the same as in the stored values of
        :meth:`~meshterm.persistence.repository.Repository.kind_counts`. Messages and acks
        have their own classes. The return value is a copy, which the caller can change
        safely.
        """
        return dict(self._kind_counts)

    def _count_packet(self, event: MeshEvent) -> None:
        """Count one packet in the current activity bucket (and remove the old buckets)."""
        bucket = int(time.time() // ACTIVITY_BUCKET_S)
        self._activity[bucket] = self._activity.get(bucket, 0) + 1
        self._activity_stamp += 1  # invalidates the cached histogram tuple
        obs = event.observation
        kind = obs.kind if obs is not None else event.kind.value
        if obs is not None and kind == "packet":
            # Put a classed raw frame in the bucket of its payload class (the shape of the
            # stored values).
            typename = (obs.raw or {}).get("payload_typename")
            if typename:
                kind = f"packet:{typename}"
        self._kind_counts[kind] = self._kind_counts.get(kind, 0) + 1
        if len(self._activity) > ACTIVITY_BUCKETS + 1:
            cutoff = bucket - ACTIVITY_BUCKETS
            for stale in [b for b in self._activity if b < cutoff]:
                del self._activity[stale]

    async def start(self) -> None:
        """Start to store heard observations in the history. Idempotent.

        This method registers the recording subscription on the event hub, and does not
        touch the device. Thus it is safe (and costs little) to call it before a
        connection exists. Observations arrive as soon as the hub operates. The
        ``monitor`` run row that holds them is opened only at the first observation. Thus
        a session that hears nothing leaves no empty run in the history.
        """
        if self.active:
            return
        self._run_start_count = self._session_count
        self._queue = asyncio.Queue()
        self._worker = asyncio.ensure_future(self._process_observations())

        def on_event(event: MeshEvent) -> None:
            # This callback runs on the event loop when packets arrive. Keep it fast, and do
            # not block. It only gives the observation to the worker queue. The worker
            # opens the run row and writes to the database. Thus a burst of heard packets
            # (each advert, telemetry, or RX-log packet that the mesh makes) can never stop
            # the render and input loop, as a synchronous commit for each packet can.
            obs = event.observation
            if obs is None:
                return
            self._session_count += 1
            queue = self._queue
            if queue is not None:
                queue.put_nowait(obs)

        self._unsubscribe = self._ctx.events.subscribe(on_event, EventKind.OBSERVATION)
        # A second subscription, with no kind filter, feeds the activity indicator in the
        # header: each packet that the hub hears goes in a one-minute bucket
        # (``ACTIVITY_BUCKET_S``), only in memory.
        self._count_unsubscribe = self._ctx.events.subscribe(self._count_packet)
        self._ctx.log.info("passive monitor recording")

    async def _process_observations(self) -> None:
        """Store the queued observations one at a time, and open the run row at the first one.

        One worker empties the queue. Thus the recording never races the creation of the
        run row, and the database write never runs inline with the synchronous event
        dispatch of the hub (refer to :meth:`start`). This is the same design as the
        inbound worker of :class:`~meshterm.services.chat_service.ChatService`.
        """
        assert self._queue is not None
        while True:
            obs = await self._queue.get()
            try:
                if self._run_id is None:
                    self._run_id = self._ctx.repo.start_run(
                        "monitor", {"mode": "background"}, self._ctx.profile_name
                    )
                self._ctx.repo.record_observation(self._run_id, obs)
            except Exception as exc:  # noqa: BLE001 - a failed write must never stop the capture
                self._ctx.log.debug("monitor: failed to record observation: %s", exc)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        """Stop the storage of observations in the history, and close the run record.

        Idempotent. If the service does not record, this method does nothing. The event
        hub continues to listen. Only the recording subscription of this service and its
        recording worker are removed.
        """
        if not self.active:
            return
        try:
            assert self._unsubscribe is not None
            self._unsubscribe()
        finally:
            self._unsubscribe = None
        if self._count_unsubscribe is not None:
            self._count_unsubscribe()
            self._count_unsubscribe = None
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        self._queue = None
        if self._run_id is not None:
            captured = self._session_count - self._run_start_count
            self._ctx.repo.finish_run(self._run_id, "ok", {"observations": captured})
            self._ctx.log.info("passive monitor stopped (run %s, %s pkts)", self._run_id, captured)
            self._run_id = None

    async def aclose(self) -> None:
        """Stop the capture at the end of the session."""
        await self.stop()
