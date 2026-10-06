# SPDX-License-Identifier: Apache-2.0
"""The Watchtower: passive rules on the packets that the hub already hears, which raise alerts.

The Watchtower is a sentinel, not a prober: it transmits nothing, and it asks the device
for nothing. It uses the :class:`~meshterm.services.event_hub.EventHub`, which is always
on, as the monitor does. It watches for the problems that are most important to a user:

* **silence**: MeshTerm has not heard a starred node for its configured threshold. The
  rule fires one time for each silent period (latched in the store). It arms again when
  the node comes back, and then it also raises a friendly **recovered** note.
* **SNR sag**: the reception of a starred node becomes worse. The median of its last few
  readings is much lower than the median of the readings before them. A cooldown
  prevents an alert each minute from a link that slowly fails.
* **new node**: a node that MeshTerm never heard before (not in the observation history
  at the start, not in the memory of the store) appeared on the mesh. The rule announces
  each id a maximum of one time, forever.

The alerts go into the :class:`~meshterm.core.watch_store.WatchStore` (which stores
them), and they give the number on the alert badge of the header. The user reads and
acknowledges them on the Watchtower screen (:mod:`meshterm.ui.watchtower_screen`). The
service is state of the session, on the :class:`~meshterm.context.AppContext`
(``ctx.watchtower``). It starts with the other services that are always on. Scripted CLI
runs never start it.
"""

from __future__ import annotations

import asyncio
import statistics
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING

from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent
from ..core.models import Observation, utcnow
from ..core.watch_store import OFF

if TYPE_CHECKING:
    from ..context import AppContext

#: The seconds between rule sweeps (the silence rule depends on time, not on packets).
SWEEP_S = 30.0

#: The number of recent SNR readings that make the "now" side of the sag comparison.
SNR_WINDOW = 4

#: How much lower (dB) the recent median must be than the prior median, for an alert.
SNR_SAG_DB = 6.0

#: The seconds before the sag rule can fire again for the same node.
SNR_COOLDOWN_S = 6 * 3600.0

#: The number of SNR readings kept for each node (the comparison uses this number at most).
_SNR_KEEP = 12


class WatchtowerService:
    """Watches the starred nodes and all the nodes on the mesh, and raises alerts.

    The attributes are private. Use the properties and the async lifecycle methods
    (:meth:`start`, :meth:`stop`, :meth:`aclose`). The rule evaluation is in the
    synchronous :meth:`note` and :meth:`evaluate`, so that tests can call it directly.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize the (idle) service.

        Args:
            ctx: The shared application context (store, repository, event hub).
        """
        self._ctx = ctx
        self._unsubscribe: Unsubscribe | None = None
        self._task: asyncio.Task | None = None
        #: All the node ids that MeshTerm ever heard (DB history + store memory + this
        #: session). The new-node rule compares against this baseline. ``None`` until the
        #: service starts.
        self._known: set[str] | None = None
        #: The rolling SNR readings of each watched node, for this session only.
        self._snr: dict[str, deque[float]] = {}
        #: The last time that the sag rule fired for each node (the cooldown clock).
        self._snr_fired: dict[str, datetime] = {}

    @property
    def active(self) -> bool:
        """Whether the sentinel watches now."""
        return self._unsubscribe is not None

    def unacked_count(self) -> int:
        """The alerts that wait for an acknowledgement: the number on the header badge."""
        return self._ctx.watch_store.unacked_count()

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        """Start to watch. Idempotent, with no device use, and safe before a connection.

        Fills the new-node baseline from the observation history (each id that the DB
        ever heard), and from the memory of the store about past announcements. Then
        subscribes to the hub, and starts the sweep. As for the monitor, packets come in
        each time the hub pumps. This includes a hub that opens later, only when it is
        necessary.
        """
        if self.active:
            return
        store = self._ctx.watch_store
        baseline = {h.node for h in self._ctx.repo.heard_nodes() if h.node}
        baseline |= set(store.state.known)
        baseline |= set(store.watched())
        self._known = baseline

        def on_event(event: MeshEvent) -> None:
            obs = event.observation
            if obs is not None:
                try:
                    self.note(obs)
                except Exception as exc:  # noqa: BLE001 - a rule bug must not kill the hub
                    self._ctx.log.debug("watchtower: note failed: %s", exc)

        self._unsubscribe = self._ctx.events.subscribe(on_event, EventKind.OBSERVATION)
        self._task = asyncio.ensure_future(self._sweep())
        self._ctx.log.info("watchtower watching (%d nodes starred)", len(store.watched()))

    async def stop(self) -> None:
        """Stop the watch, and flush the pending writes of the store. Idempotent."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass
            self._task = None
        self._ctx.watch_store.flush()

    async def aclose(self) -> None:
        """Stop the watch at the end of the session."""
        await self.stop()

    async def _sweep(self) -> None:
        """Run the rules that depend on time, at a slow heartbeat."""
        while True:
            await asyncio.sleep(SWEEP_S)
            try:
                self.evaluate()
            except Exception as exc:  # noqa: BLE001 - keep the sentinel alive
                self._ctx.log.debug("watchtower: sweep failed: %s", exc)

    # --- packet-driven rules -------------------------------------------------------------

    def note(self, obs: Observation) -> None:
        """Add one observation to the rules (called for each hub packet, so keep it fast).

        Args:
            obs: The observation that MeshTerm heard.
        """
        node = obs.node
        if not node:
            return
        store = self._ctx.watch_store
        entry = store.watched().get(node)

        # New node: not in the DB, the memory of the store, or this session before now.
        # A star on a node is also an introduction, so watched nodes are never "new".
        if self._known is not None and node not in self._known:
            self._known.add(node)
            store.remember_known(node)
            if entry is None and store.new_node_alerts:
                label = obs.name or node
                store.add_alert(
                    "new-node",
                    label,
                    "first appearance — never heard on this mesh before",
                    when=obs.observed_at,
                )

        if entry is None:
            return
        was_silent = entry.silent_since is not None
        store.note_heard(node, when=obs.observed_at, name=obs.name)
        if was_silent:
            store.clear_silent(node)
            store.add_alert(
                "recovered",
                entry.name,
                "back on the air — heard again after a silence alarm",
                when=obs.observed_at,
            )
        # Packet-kind rows measure our link to the last relay, not to the node. Thus they
        # prove that the node is alive (above), but they tell nothing about the signal of
        # the node itself.
        if entry.snr_watch and obs.snr is not None and obs.kind != "packet":
            self._track_snr(node, entry.name, obs)

    def _track_snr(self, node: str, label: str, obs: Observation) -> None:
        """Add one SNR reading, and fire the sag rule when the trend is bad enough."""
        readings = self._snr.setdefault(node, deque(maxlen=_SNR_KEEP))
        readings.append(float(obs.snr))  # type: ignore[arg-type]
        if len(readings) < 2 * SNR_WINDOW:
            return
        recent = statistics.median(list(readings)[-SNR_WINDOW:])
        prior = statistics.median(list(readings)[:-SNR_WINDOW])
        if prior - recent < SNR_SAG_DB:
            return
        fired = self._snr_fired.get(node)
        now = obs.observed_at or utcnow()
        if fired is not None and (now - fired).total_seconds() < SNR_COOLDOWN_S:
            return
        self._snr_fired[node] = now
        self._ctx.watch_store.add_alert(
            "snr",
            label,
            f"reception sagging — median {prior:+.1f} dB → {recent:+.1f} dB",
            when=now,
        )

    # --- time-driven rules ---------------------------------------------------------------

    def evaluate(self, now: datetime | None = None) -> None:
        """Run the silence rule on each watched node, and flush the store.

        Args:
            now: The evaluation time. The default is the current time. Tests give their
                own time.
        """
        now = now or utcnow()
        store = self._ctx.watch_store
        for key, entry in store.watched().items():
            hours = entry.silence_hours
            if hours == OFF or entry.last_heard is None or entry.silent_since is not None:
                continue
            quiet_s = (now - entry.last_heard).total_seconds()
            if quiet_s >= hours * 3600:
                store.mark_silent(key, now)
                store.add_alert(
                    "silence",
                    entry.name,
                    f"nothing heard for {hours} h",
                    when=now,
                )
        store.flush()
