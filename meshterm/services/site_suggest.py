# SPDX-License-Identifier: Apache-2.0
"""The site suggestion: notice that the radio hears the way out of another site, and offer it.

The outbound paths of a site apply while the site is active (refer to
:mod:`meshterm.core.outbound_store`). The user takes the computer from site to site, and can
forget to change the active site. This service listens to what our node hears after the
session starts. A packet that arrives through a repeater names that repeater: the last hop
of its relay path, or the node itself for a packet with no relay. When the first hop of an
outbound path of another site is heard :data:`HEARD_TO_SUGGEST` times, and no first hop of
the active site was heard, the service has a suggestion. The menu then asks the user, and
says what MeshTerm heard (refer to :func:`meshterm.ui.outbound_screen.offer_site`). The
service never changes the active site by itself.

It asks at most one time in a session. When the radio hears a first hop of the active site
first, the question is settled: the user is where the active site says. It does not ask
for a fixed device, which keeps its own paths, or when the ``suggest_site`` preference is
off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.events import EventKind, MeshEvent
from ..core.models import Observation
from ..core.outbound_store import Site
from ..persistence.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from ..context import AppContext

#: How many packets from the first hop of another site make a suggestion. One packet can be
#: a hash that two nodes share. Three are a node that is really near.
HEARD_TO_SUGGEST = 3


@dataclass(frozen=True, slots=True)
class SiteSuggestion:
    """A site that the radio seems to be at.

    Attributes:
        site: The name of the site.
        hop: The first hop of its outbound path that the radio heard, as the store keeps it.
        heard: How many packets came through that hop.
    """

    site: str
    hop: str
    heard: int


def heard_hop(obs: Observation) -> str | None:
    """The node that our node heard transmit this packet, as a hex hash, or ``None``.

    For a packet with relays, it is the last relay. For a packet with no relay, it is the
    node that sent it. Other classes of observation do not say who transmitted the packet
    that our node received, so they give ``None``.
    """
    if obs.kind != "packet" or obs.path is None:
        return None
    hops = [hop for hop in obs.path.split(",") if hop]
    if hops:
        return hops[-1].lower()
    return obs.node.lower() if obs.node else None


def _matches(heard: str, hop: str) -> bool:
    """Whether a heard hash can be the stored hop: one is a prefix of the other."""
    return bool(heard) and (hop.startswith(heard) or heard.startswith(hop))


class SiteWatch:
    """The decision of the service, with no I/O: count what is heard, and find a suggestion.

    Args:
        sites: The sites of this computer.
        active: The name of the active site, or ``None``.
        threshold: How many packets from one first hop make a suggestion.
    """

    def __init__(
        self, sites: list[Site], active: str | None, *, threshold: int = HEARD_TO_SUGGEST
    ) -> None:
        """Index the first hops of each site."""
        self._active = active.casefold() if active else None
        self._threshold = threshold
        self._own = [
            path[0] for site in sites if site.name.casefold() == self._active for path in site.paths
        ]
        self._others = [
            (site.name, path[0])
            for site in sites
            if site.name.casefold() != self._active
            for path in site.paths
        ]
        self._counts: dict[tuple[str, str], int] = {}
        self.settled = not self._others

    def note(self, heard: str) -> SiteSuggestion | None:
        """Count one heard hash. Return a suggestion when one is due, else ``None``.

        After a suggestion, or after a first hop of the active site is heard, the watch is
        settled, and it returns nothing more.
        """
        if self.settled or not heard:
            return None
        if any(_matches(heard, hop) for hop in self._own):
            self.settled = True
            return None
        for name, hop in self._others:
            if _matches(heard, hop):
                count = self._counts.get((name, hop), 0) + 1
                self._counts[(name, hop)] = count
                if count >= self._threshold:
                    self.settled = True
                    return SiteSuggestion(name, hop, count)
                return None
        return None


class SiteSuggester:
    """The session service: it listens to the hub and keeps one pending suggestion.

    The interactive session starts it with the other always-on services. The menu takes
    the pending suggestion (:meth:`take`) and asks the user. :attr:`on_ready` lets the menu
    ask at once when it is on the screen.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Make the service idle."""
        self._ctx = ctx
        self._watch: SiteWatch | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._pending: SiteSuggestion | None = None
        #: A callback that the menu sets. The service calls it when a suggestion is ready.
        self.on_ready: Callable[[], None] | None = None

    async def start(self) -> None:
        """Start to listen, unless the preference is off or there is nothing to suggest."""
        if self._unsubscribe is not None or not self._ctx.preferences.suggest_site:
            return
        store = self._ctx.outbound_store
        active = store.active_site()
        self._watch = SiteWatch(store.sites(), active.name if active else None)
        if self._watch.settled:
            return
        self._unsubscribe = self._ctx.events.subscribe(self._on_event, EventKind.OBSERVATION)

    def _on_event(self, event: MeshEvent) -> None:
        """Count the hop that transmitted one packet. This runs on each packet, so it is cheap."""
        obs = event.observation
        if obs is None or self._watch is None:
            return None
        hop = heard_hop(obs)
        if hop is None:
            return None
        suggestion = self._watch.note(hop)
        if self._watch.settled:
            self._stop()
        if suggestion is not None:
            return self._decide(suggestion)  # the hub runs the coroutine as a task
        return None

    async def _decide(self, suggestion: SiteSuggestion) -> None:
        """Keep the suggestion, unless the connected device is fixed or the user said never."""
        if not self._ctx.preferences.suggest_site:
            return
        try:
            info = await self._ctx.devstate.self_info()
        except Exception:  # noqa: BLE001 - with no device identity, only the sites apply
            info = {}
        key = str(info.get("public_key") or "")
        if key and self._ctx.outbound_store.fixed_device(key) is not None:
            return  # a fixed device keeps its own paths: the active site does not apply
        get_logger().info(
            "heard %s %d times: it is a first hop of site %s",
            suggestion.hop,
            suggestion.heard,
            suggestion.site,
        )
        self._pending = suggestion
        if self.on_ready is not None:
            self.on_ready()

    def take(self) -> SiteSuggestion | None:
        """Return the pending suggestion one time, and forget it."""
        suggestion, self._pending = self._pending, None
        return suggestion

    def _stop(self) -> None:
        """Stop listening."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    async def aclose(self) -> None:
        """Stop listening at the end of the session."""
        self._stop()
