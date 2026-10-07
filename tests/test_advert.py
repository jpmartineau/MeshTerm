# SPDX-License-Identifier: Apache-2.0
"""Tests for the weekly flood advert.

The tests cover these parts:

- The week arithmetic of the store: the switch-on, the marks of each device, and the rule
  that the latest mark wins.
- The wait of the scheduler for a live link and a quiet spell.
- The manual paths that restart the week: a flood advert that the user sends by hand, and
  the switch-on of the preference.
"""

from __future__ import annotations

import asyncio
import io
import json
import random
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core import transmit_gate
from meshterm.core.admin_store import AdminStore
from meshterm.core.advert_store import WEEK, AdvertStore
from meshterm.core.config import Settings
from meshterm.core.connection import make_device
from meshterm.core.device_store import DeviceStore
from meshterm.core.models import utcnow
from meshterm.persistence.repository import Repository
from meshterm.services.advert_scheduler import AdvertScheduler
from meshterm.tools.config import apply_ops

KEY = "00" * 32  # the simulator's public key
OTHER = "ab" * 32


# -- the store -----------------------------------------------------------------------


def test_nothing_is_due_while_off(tmp_path: Path) -> None:
    """If the user never switched the preference on, an armed device is never due."""
    store = AdvertStore(tmp_path / "adverts.json")
    store.arm(KEY, when=utcnow() - timedelta(days=60))
    assert store.enabled_since() is None
    assert store.due_at(KEY) is None
    assert not store.due(KEY)


def test_switching_on_starts_the_week(tmp_path: Path) -> None:
    """A device that was armed long ago still waits a full week from the switch-on."""
    store = AdvertStore(tmp_path / "adverts.json")
    t0 = utcnow()
    store.arm(KEY, when=t0 - timedelta(days=30))
    store.set_enabled(True, when=t0)
    assert store.due_at(KEY) == t0 + WEEK
    assert not store.due(KEY, now=t0 + WEEK - timedelta(seconds=1))
    assert store.due(KEY, now=t0 + WEEK)


def test_switching_on_again_restarts_the_week(tmp_path: Path) -> None:
    """Each switch-on is a new start, also when the store has a switch-on already."""
    store = AdvertStore(tmp_path / "adverts.json")
    t0 = utcnow()
    store.arm(KEY, when=t0 - timedelta(days=30))
    store.set_enabled(True, when=t0 - timedelta(days=10))
    store.set_enabled(True, when=t0)
    assert store.due_at(KEY) == t0 + WEEK


def test_a_flood_advert_pushes_the_week_out(tmp_path: Path) -> None:
    """The next weekly advert is a full week after the last flood advert."""
    store = AdvertStore(tmp_path / "adverts.json")
    t0 = utcnow()
    store.set_enabled(True, when=t0)
    store.arm(KEY, when=t0)
    store.mark_flood(KEY, when=t0 + timedelta(days=3))
    assert store.due_at(KEY) == t0 + timedelta(days=3) + WEEK


def test_each_device_keeps_its_own_week(tmp_path: Path) -> None:
    """The use of another device does not reset the week of this device and does not borrow it."""
    store = AdvertStore(tmp_path / "adverts.json")
    t0 = utcnow() - timedelta(days=20)
    store.set_enabled(True, when=t0)
    store.arm(KEY, when=t0)
    store.arm(OTHER, when=t0 + timedelta(days=2))
    store.mark_flood(OTHER, when=t0 + timedelta(days=9))
    assert store.due_at(KEY) == t0 + WEEK
    assert store.due_at(OTHER) == t0 + timedelta(days=9) + WEEK


def test_arming_never_moves_an_existing_mark(tmp_path: Path) -> None:
    """Arm is for the first connection only. A reconnect does not restart the week."""
    store = AdvertStore(tmp_path / "adverts.json")
    t0 = utcnow() - timedelta(days=5)
    store.arm(KEY, when=t0)
    store.arm(KEY)
    assert store.week_began(KEY) == t0


def test_switching_off_clears_the_switch(tmp_path: Path) -> None:
    """Off removes the switch-on time, so the next on starts a new week."""
    store = AdvertStore(tmp_path / "adverts.json")
    store.set_enabled(True)
    store.set_enabled(False)
    assert store.enabled_since() is None


def test_sync_catches_a_hand_edit(tmp_path: Path) -> None:
    """A preference that is on, with no switch-on time, starts its week now. Off clears the time."""
    store = AdvertStore(tmp_path / "adverts.json")
    store.sync_enabled(True)
    first = store.enabled_since()
    assert first is not None
    store.sync_enabled(True)
    assert store.enabled_since() == first  # if the two values agree, nothing is written
    store.sync_enabled(False)
    assert store.enabled_since() is None


def test_an_old_shaped_file_reads_empty(tmp_path: Path) -> None:
    """The store ignores the records of the cadence of each device, which this store replaced."""
    path = tmp_path / "adverts.json"
    path.write_text(json.dumps({KEY: {"flood_hours": 24, "last_flood": utcnow().isoformat()}}))
    store = AdvertStore(path)
    assert store.week_began(KEY) is None
    store.arm(KEY)
    written = json.loads(path.read_text())
    assert set(written) == {"enabled_since", "devices"}
    assert KEY not in written  # the old top-level record is removed


def test_a_write_keeps_only_the_fields_the_store_knows(tmp_path: Path) -> None:
    """Unknown fields, retired or invented, do not survive the next write."""
    path = tmp_path / "adverts.json"
    path.write_text(
        json.dumps(
            {
                "enabled_since": None,
                "stray": 1,
                "devices": {KEY: {"since": utcnow().isoformat(), "flood_hours": 24}},
            }
        )
    )
    store = AdvertStore(path)
    store.mark_flood(OTHER)
    written = json.loads(path.read_text())
    assert "stray" not in written
    assert written["devices"][KEY].keys() == {"since"}


# -- the scheduler against the simulator ---------------------------------------------


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """An application context that uses the mock device, for the scheduler and executor tests."""
    transmit_gate.current().reset()
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "adv.db")
    context = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()
    transmit_gate.current().reset()


class _Rng(random.Random):
    """A random source whose ``uniform`` returns a fixed sequence, and counts the draws."""

    def __init__(self, draws: Sequence[float]) -> None:
        super().__init__()  # one argument: Random.__new__ of Python 3.10 does not accept more
        self.draws = list(draws)
        self.calls = 0

    def uniform(self, a: float, b: float) -> float:  # noqa: D102
        self.calls += 1
        return self.draws[min(self.calls, len(self.draws)) - 1]


class _Clock:
    """A monotonic clock that the test advances, on the timeline of the transmit gate.

    The start of the clock is far in the past. Thus a time that the test stamps on the gate
    is also behind the real clock, and it never looks like a cooldown that still runs.
    """

    def __init__(self) -> None:
        self.base = time.monotonic() - 100_000.0
        self.t = 0.0

    def __call__(self) -> float:
        return self.base + self.t


async def _due_scheduler(ctx: AppContext, *draws: float) -> tuple[AdvertScheduler, _Clock, list]:
    """A scheduler for a connected simulator. The weekly advert of the simulator is due."""
    device = await ctx.device()
    ctx.preferences.set("weekly_flood_advert", True)
    week_ago = utcnow() - WEEK - timedelta(hours=1)
    ctx.advert_store.set_enabled(True, when=week_ago)
    ctx.advert_store.arm(KEY, when=week_ago)
    clock = _Clock()
    scheduler = AdvertScheduler(ctx, rng=_Rng(draws or (0.0,)), clock=clock)
    sent: list[bool] = []
    device.send_advert = lambda flood=False: _record(sent, flood)  # type: ignore[method-assign]
    return scheduler, clock, sent


async def test_a_due_advert_waits_to_hear_the_mesh(ctx: AppContext) -> None:
    """A due advert waits to hear the mesh.

    The air can be silent for a long time. Nothing goes out until the scheduler hears a
    packet.
    """
    scheduler, clock, sent = await _due_scheduler(ctx)
    clock.t = 3600.0
    await scheduler._pass()
    assert sent == []


async def test_a_due_advert_goes_out_after_the_quiet_spell(ctx: AppContext) -> None:
    """A due advert goes out after the quiet spell.

    After a packet, the quiet time of the preference plus the random extension passes. Then
    the scheduler sends the advert.
    """
    scheduler, clock, sent = await _due_scheduler(ctx, 2.0)
    scheduler._on_event(None)  # type: ignore[arg-type]  # a packet at t=0
    clock.t = 31.9  # 30 s default + 2 s drawn
    await scheduler._pass()
    assert sent == []
    clock.t = 32.0
    await scheduler._pass()
    assert sent == [True]
    assert not ctx.advert_store.due(KEY)  # the send restarted the week
    clock.t = 100.0
    await scheduler._pass()
    assert sent == [True]  # one time, and not one time for each tick


async def test_the_quiet_spell_follows_the_preference(ctx: AppContext) -> None:
    """A preference of 5 s sends five seconds (plus the draw) after the last packet."""
    scheduler, clock, sent = await _due_scheduler(ctx, 0.0)
    ctx.preferences.set("advert_quiet_s", 5)
    scheduler._on_event(None)  # type: ignore[arg-type]
    clock.t = 5.0
    await scheduler._pass()
    assert sent == [True]


async def test_every_break_in_the_silence_redraws_the_extension(ctx: AppContext) -> None:
    """Each break in the silence draws the extension again.

    A packet in the middle of the wait restarts the quiet time and draws a new random part.
    """
    rng_draws = (1.0, 4.0)
    scheduler, clock, sent = await _due_scheduler(ctx, *rng_draws)
    scheduler._on_event(None)  # type: ignore[arg-type]  # t=0
    clock.t = 20.0
    await scheduler._pass()
    scheduler._on_event(None)  # type: ignore[arg-type]  # the silence breaks at t=20
    clock.t = 20.0 + 31.0  # enough for the first draw, but not for the second
    await scheduler._pass()
    assert sent == []
    clock.t = 20.0 + 34.0
    await scheduler._pass()
    assert sent == [True]
    assert scheduler._rng.calls == 2  # type: ignore[attr-defined]


async def test_our_own_transmission_breaks_the_silence(ctx: AppContext) -> None:
    """Our own transmission breaks the silence.

    A packet that we sent counts in the same way as a packet that we heard. The quiet time
    runs from it.
    """
    scheduler, clock, sent = await _due_scheduler(ctx, 0.0)
    scheduler._on_event(None)  # type: ignore[arg-type]  # t=0
    transmit_gate.current()._last_sent = clock.base + 10.0  # we sent at t=10
    clock.t = 35.0
    await scheduler._pass()
    assert sent == []
    clock.t = 40.0
    await scheduler._pass()
    assert sent == [True]


async def test_a_reconnect_must_hear_the_mesh_again(ctx: AppContext) -> None:
    """A reconnect must hear the mesh again.

    A packet from the old link says nothing about the new link.
    """
    scheduler, clock, sent = await _due_scheduler(ctx, 0.0)
    scheduler._on_event(None)  # type: ignore[arg-type]
    await scheduler._pass()
    fresh = make_device(mock=True, port=None)
    fresh.send_advert = lambda flood=False: _record(sent, flood)  # type: ignore[method-assign]
    ctx._device = fresh  # the new Device of the reconnect flow
    clock.t = 3600.0
    await scheduler._pass()
    assert sent == []
    scheduler._on_event(None)  # type: ignore[arg-type]  # heard on the new link at t=3600
    clock.t = 3630.0
    await scheduler._pass()
    assert sent == [True]


async def test_nothing_goes_out_while_the_preference_is_off(ctx: AppContext) -> None:
    """If the preference is off, nothing goes out.

    A device that is a week overdue sends nothing, and the switch is cleared.
    """
    scheduler, clock, sent = await _due_scheduler(ctx, 0.0)
    ctx.preferences.set("weekly_flood_advert", False)
    scheduler._on_event(None)  # type: ignore[arg-type]
    clock.t = 3600.0
    await scheduler._pass()
    assert sent == []
    assert ctx.advert_store.enabled_since() is None


async def test_a_hand_sent_flood_advert_mid_wait_restarts_the_week(ctx: AppContext) -> None:
    """A flood advert that the user sends by hand during the wait restarts the week.

    The scheduler reads the file again just before it sends. Thus a manual flood after the
    check wins.
    """
    scheduler, clock, sent = await _due_scheduler(ctx, 0.0)
    scheduler._on_event(None)  # type: ignore[arg-type]
    await scheduler._pass()  # due, and the scheduler waits for quiet
    ctx.advert_store.mark_flood(KEY)
    clock.t = 40.0
    await scheduler._pass()
    assert sent == []


async def test_first_connection_arms_without_sending(ctx: AppContext) -> None:
    """A device that is new starts its week when it connects. Nothing is transmitted."""
    await ctx.device()
    ctx.preferences.set("weekly_flood_advert", True)
    ctx.advert_store.set_enabled(True, when=utcnow() - timedelta(days=30))
    clock = _Clock()
    scheduler = AdvertScheduler(ctx, rng=_Rng((0.0,)), clock=clock)
    sent: list[bool] = []
    (await ctx.device()).send_advert = lambda flood=False: _record(sent, flood)  # type: ignore[method-assign]
    scheduler._on_event(None)  # type: ignore[arg-type]
    clock.t = 3600.0
    await scheduler._pass()
    assert sent == []
    assert ctx.advert_store.week_began(KEY) is not None


async def test_scheduler_skips_quietly_when_disconnected(ctx: AppContext) -> None:
    """A pass with no device connected does nothing. It stores nothing."""
    scheduler = AdvertScheduler(ctx)
    await scheduler._pass()
    assert ctx.advert_store.week_began(KEY) is None


# -- the manual paths ----------------------------------------------------------------


class _NoteUi:
    """A minimal UI surface that only collects notes. ``apply_ops`` needs only this."""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def note(self, markup: str) -> None:
        self.notes.append(markup)

    def ack(self, markup: str) -> None:
        # The answer of the menu: an acknowledgement is a note. The scripted CLI removes it.
        self.note(markup)


async def test_a_manual_flood_advert_restarts_the_week(ctx: AppContext) -> None:
    """A flood advert through ``apply_ops`` (each manual flow) restarts the week.

    ``apply_ops`` marks the week of the device again.
    """
    device = await ctx.device()
    ctx.advert_store.set_enabled(True, when=utcnow() - timedelta(days=30))
    ctx.advert_store.arm(KEY, when=utcnow() - timedelta(days=30))
    assert ctx.advert_store.due(KEY)

    ctx._ui = _NoteUi()  # type: ignore[assignment]
    snapshot = dict(await device.get_self_info())
    await apply_ops(ctx, device, snapshot, [("advert", True)])
    assert not ctx.advert_store.due(KEY)


async def test_a_zero_hop_advert_leaves_the_week_alone(ctx: AppContext) -> None:
    """A zero-hop advert reaches only the neighbours, and it does not reset the week."""
    device = await ctx.device()
    ctx.advert_store.set_enabled(True, when=utcnow() - timedelta(days=30))
    ctx.advert_store.arm(KEY, when=utcnow() - timedelta(days=30))

    ctx._ui = _NoteUi()  # type: ignore[assignment]
    snapshot = dict(await device.get_self_info())
    await apply_ops(ctx, device, snapshot, [("advert", False)])
    assert ctx.advert_store.due(KEY)


async def test_switching_the_preference_records_the_switch(ctx: AppContext) -> None:
    """The preferences tool stores each on and off when it happens, from the page or a shell."""
    from meshterm.tools.preferences import PreferencesTool

    tool = PreferencesTool()
    await tool.run(ctx, {"ops": [("set", "weekly_flood_advert", "on")]})
    assert ctx.advert_store.enabled_since() is not None
    await tool.run(ctx, {"ops": [("set", "weekly_flood_advert", "off")]})
    assert ctx.advert_store.enabled_since() is None


async def _record(sent: list[bool], flood: bool) -> None:
    """A substitute for ``send_advert`` that stores the type of each transmission."""
    sent.append(flood)


# -- the status that a screen reads -------------------------------------------------------


async def test_status_follows_the_wait(ctx: AppContext) -> None:
    """The status goes from counting, to due and listening, to due and waiting for quiet."""
    from meshterm.services.advert_scheduler import COUNTING, LISTENING, QUIET

    scheduler, clock, _sent = await _due_scheduler(ctx, 0.0)
    scheduler._task = asyncio.get_running_loop().create_future()  # it looks as if it runs
    try:
        await scheduler._pass()
        assert scheduler.status().state == LISTENING  # type: ignore[union-attr]
        scheduler._on_event(None)  # type: ignore[arg-type]
        await scheduler._pass()
        assert scheduler.status().state == QUIET  # type: ignore[union-attr]
        clock.t = 40.0
        await scheduler._pass()  # it sent, and a new week begins
        clock.t = 80.0
        await scheduler._pass()
        status = scheduler.status()
        assert status is not None and status.state == COUNTING
        assert status.due_at is not None and status.due_at > utcnow() + WEEK - timedelta(minutes=1)
    finally:
        scheduler._task.cancel()


async def test_status_is_silent_while_off_or_stopped(ctx: AppContext) -> None:
    """The status reports nothing while the preference is off, or before the loop runs."""
    scheduler, _clock, _sent = await _due_scheduler(ctx, 0.0)
    assert scheduler.status() is None  # not running
    scheduler._task = asyncio.get_running_loop().create_future()
    try:
        ctx.preferences.set("weekly_flood_advert", False)
        assert scheduler.status() is None
    finally:
        scheduler._task.cancel()
