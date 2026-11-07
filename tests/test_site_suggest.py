# SPDX-License-Identifier: Apache-2.0
"""Tests for the site suggestion: what counts as heard, and when a suggestion is due."""

from __future__ import annotations

from meshterm.core.models import Observation
from meshterm.core.outbound_store import Site
from meshterm.services.site_suggest import HEARD_TO_SUGGEST, SiteSuggestion, SiteWatch, heard_hop

HOME = Site("Home", [("3d63c6429436",), ("27d4396a2967", "f2c24f54551e")])
LAB = Site("Foulab", [("a100008dc55e", "b2b2")])


def test_the_heard_node_is_the_last_relay_or_the_sender() -> None:
    """A relayed packet names its last relay. A direct packet names its sender."""
    assert heard_hop(Observation(node="aa", kind="packet", path="11,22,A1")) == "a1"
    assert heard_hop(Observation(node="BB", kind="packet", path="")) == "bb"
    assert heard_hop(Observation(node="cc", kind="advert", path=None)) is None
    assert heard_hop(Observation(node=None, kind="packet", path="")) is None


def test_three_packets_from_another_sites_first_hop_make_a_suggestion() -> None:
    """The first hop of the Foulab path, heard three times, suggests Foulab."""
    watch = SiteWatch([HOME, LAB], "Home")
    for _ in range(HEARD_TO_SUGGEST - 1):
        assert watch.note("a1") is None  # a 1-byte hash matches the stored key prefix
    assert watch.note("a100") == SiteSuggestion("Foulab", "a100008dc55e", HEARD_TO_SUGGEST)
    assert watch.settled
    assert watch.note("a1") is None  # one question for each session


def test_a_first_hop_of_the_active_site_settles_the_question() -> None:
    """Once the radio hears the way out of the active site, there is nothing to suggest."""
    watch = SiteWatch([HOME, LAB], "home")
    watch.note("a1")
    assert watch.note("27d4") is None
    assert watch.settled
    assert all(watch.note("a1") is None for _ in range(5))


def test_a_hop_that_is_not_a_first_hop_counts_for_nothing() -> None:
    """Only the first hop of a path says where the radio is: the second hop is far away."""
    watch = SiteWatch([HOME, LAB], "Home")
    assert all(watch.note("b2b2") is None for _ in range(5))
    assert not watch.settled


async def test_the_service_keeps_one_suggestion_for_the_menu(tmp_path) -> None:  # noqa: ANN001
    """Packets from the hub make a pending suggestion, unless the connected device is fixed."""
    from types import SimpleNamespace

    from meshterm.core.events import MeshEvent
    from meshterm.core.outbound_store import OutboundStore
    from meshterm.services.site_suggest import SiteSuggester

    store = OutboundStore(tmp_path / "o.json")
    store.put_site("Home", list(HOME.paths))
    store.put_site("Foulab", list(LAB.paths))
    store.set_active("Home")
    handlers: list = []

    async def self_info() -> dict:
        return {"public_key": "c0" * 32}

    ctx = SimpleNamespace(
        preferences=SimpleNamespace(suggest_site=True),
        outbound_store=store,
        events=SimpleNamespace(subscribe=lambda handler, *kinds: handlers.append(handler) or None),
        devstate=SimpleNamespace(self_info=self_info),
    )
    service = SiteSuggester(ctx)
    ready: list = []
    service.on_ready = lambda: ready.append(True)
    await service.start()
    packet = MeshEvent.observation_event(Observation(node="99", kind="packet", path="77,a1"))
    pending = [handlers[0](packet) for _ in range(HEARD_TO_SUGGEST)]
    assert pending[:-1] == [None, None]
    await pending[-1]
    assert ready == [True]
    assert service.take() == SiteSuggestion("Foulab", "a100008dc55e", HEARD_TO_SUGGEST)
    assert service.take() is None  # one time only

    store.fix_device("c0" * 32, "Roof", [])
    fixed = SiteSuggester(ctx)
    handlers.clear()
    await fixed.start()
    last = [handlers[0](packet) for _ in range(HEARD_TO_SUGGEST)][-1]
    await last
    assert fixed.take() is None  # the active site does not apply to a fixed device


def test_with_no_active_site_any_site_can_be_suggested_and_one_site_needs_no_watch() -> None:
    """No active site: the first site heard is suggested. A single active site settles at once."""
    watch = SiteWatch([HOME, LAB], None)
    suggestions = [watch.note("3d") for _ in range(HEARD_TO_SUGGEST)]
    assert suggestions[-1] == SiteSuggestion("Home", "3d63c6429436", HEARD_TO_SUGGEST)
    assert SiteWatch([HOME], "Home").settled
