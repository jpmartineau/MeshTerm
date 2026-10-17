# SPDX-License-Identifier: Apache-2.0
"""The demo world: the same history at each run, and a device that agrees with it."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from meshterm.core.channels import channel_identity
from meshterm.core.models import utcnow
from meshterm.demo import history, world
from meshterm.demo.device import DemoDevice
from meshterm.demo.home import prepare
from meshterm.persistence.repository import Repository


def _seed(path: Path, now) -> Repository:  # noqa: ANN001 - a datetime
    repo = Repository(path)
    history.seed(repo, now)
    return repo


def test_each_node_has_its_own_hash() -> None:
    """No two nodes of the world share a path hash, so each hop resolves to one node."""
    hashes = [node.hash for node in (world.SELF, *world.NODES)]
    assert len(hashes) == len(set(hashes))


def test_the_history_is_the_same_at_each_run(tmp_path: Path) -> None:
    """Two runs give the same rows, with the times moved by the gap between the two starts."""
    now = utcnow()
    first = _seed(tmp_path / "a.db", now)
    second = _seed(tmp_path / "b.db", now + timedelta(hours=5))
    assert first.table_counts() == second.table_counts()
    assert first.kind_counts() == second.kind_counts()
    public = world.CHANNELS[0]
    identity = channel_identity(public.name, public.secret)

    def lines(repo: Repository, start) -> list[tuple[str, int]]:  # noqa: ANN001 - a datetime
        rows = repo.recent_chat_messages(is_channel=True, channel_id=identity, limit=500)
        return [(m.text, round((start - m.created_at).total_seconds())) for m in rows]

    assert lines(first, now) == lines(second, now + timedelta(hours=5))


def test_a_channel_line_has_its_heard_copies(tmp_path: Path) -> None:
    """The packets of a channel line decrypt to the line, so its paths can be found."""
    from meshterm.services.message_paths import channel_arrivals

    repo = _seed(tmp_path / "h.db", utcnow())
    public = world.CHANNELS[0]
    identity = channel_identity(public.name, public.secret)
    last = repo.recent_chat_messages(is_channel=True, channel_id=identity, limit=1)[-1]
    arrivals = channel_arrivals(repo, last, channel_name=public.name, secret=public.secret)
    assert arrivals


def test_the_demo_device_traces_a_path() -> None:
    """A forced path through linked repeaters comes back with a reading for each link."""

    async def run():  # noqa: ANN202
        device = DemoDevice()
        await device.connect()
        spec = ",".join(world.node(n).hash for n in ("Rosemont", "Mont-Royal", "Rosemont"))
        for _ in range(10):
            result = await device.run_trace("(path)", path=spec)
            if result.success:
                return result
        return result

    result = asyncio.run(run())
    assert result.success
    assert [h.node for h in result.hops] == ["d8", "2a", "d8", None]


def test_a_tuned_repeater_is_heard_best_at_its_best_power() -> None:
    """TX optimize finds a peak: far from its best power, a relay loses the target."""
    from meshterm.demo.device import REMOTE_TX_BEST

    async def heard(tx: int) -> list[float]:
        device = DemoDevice()
        await device.connect()
        rosemont = next(c for c in await device.get_contacts() if c.name == "Rosemont")
        await device.admin_login(rosemont, world.ADMIN_PASSWORD)
        await device.set_remote_tx_power(rosemont, tx)
        spec = ",".join(world.node(n).hash for n in ("Rosemont", "Mont-Royal", "Rosemont"))
        results = [await device.run_trace("(path)", path=spec) for _ in range(5)]
        return [result.hops[1].snr for result in results if result.success]

    best = asyncio.run(heard(REMOTE_TX_BEST))
    far = asyncio.run(heard(REMOTE_TX_BEST + 8))
    assert best
    assert not far or max(far) < min(best)


def test_prepare_writes_a_home(tmp_path: Path) -> None:
    """The demo home gets a database and the stores of the joined room and the alerts."""
    home = tmp_path / "demo"
    prepare(home, utcnow())
    prepare(home, utcnow())  # a second start writes it again over the first
    assert (home / "meshterm.db").exists()
    assert (home / "rooms.json").exists()
    assert (home / "watchtower.json").exists()


def test_each_line_is_sent_while_its_sender_transmits() -> None:
    """No node speaks before our node first heard it, or after it went quiet."""
    from meshterm.demo import script

    groups = (script.CHANNEL_LINES, script.DIRECT_LINES, script.ROOM_LINES)
    for group in groups:
        for lines in group.values():
            for line in lines:
                node = world.BY_NAME.get(line.sender)
                if node is None or node is world.SELF:
                    continue  # an author that no contact names
                age_h = line.age_min / 60.0
                assert node.quiet_hours is not None, line
                assert node.quiet_hours <= age_h <= node.first_heard_days * 24.0, line


def test_our_node_uses_the_path_width_of_the_mesh() -> None:
    """Our node uses three bytes for each hop, so a trace uses two (the widest it encodes)."""
    from meshterm.ui.trace_screen import collapse_trace_width

    mode = asyncio.run(DemoDevice().get_path_hash_mode())
    assert mode + 1 == world.path_width(world.SELF) == 3
    assert collapse_trace_width(mode) == 2


def test_a_mentioned_node_answers(monkeypatch) -> None:  # noqa: ANN001 - the pytest fixture
    """A channel message that mentions Dave gets his answer, which mentions our node."""
    from meshterm.demo import script

    monkeypatch.setattr(script, "REPLY_DELAY_S", 0.0)

    async def run():  # noqa: ANN202
        device = DemoDevice()
        await device.connect()
        messages = []

        def on_event(event) -> None:  # noqa: ANN001 - a MeshEvent
            if event.message is not None:
                messages.append(event.message)

        unsubscribe = await device.subscribe_events(on_event)
        await device.send_channel_message(0, "Hey, is @[Dave-Lachine] coming?")
        await device.send_channel_message(0, "Hello, @[Félix]")  # no answer in the script
        for _ in range(100):
            if messages:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        unsubscribe()
        return messages

    messages = asyncio.run(run())
    assert [m.text for m in messages] == [f"Dave-Lachine: {script.REPLIES['Dave-Lachine']}"]
    assert messages[0].is_channel and messages[0].channel == 0
    assert f"@[{world.SELF.name}]" in messages[0].text
