# SPDX-License-Identifier: Apache-2.0
"""The SPI radio's node, run for real: the radio library with a fake radio underneath.

MeshTerm's own companion client talks to it over the loopback port exactly as it would on a
uConsole. This is the only way to prove what the node exists for — that a channel written
through MeshTerm is still there after the node restarts, which is the bug the node's state
directory ended. Skipped where ``openhop_core`` isn't installed (it is in the dev extra).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from meshterm.core import radionode

openhop_core = pytest.importorskip("openhop_core")


class _FakeRadio:
    """Just enough of an SX1262 for the node to come up and sit listening."""

    instances: list[_FakeRadio] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.preamble_length = kwargs.get("preamble_length", 12)
        self.cleaned = False
        _FakeRadio.instances.append(self)

    def begin(self) -> bool:
        return True

    def cleanup(self) -> None:
        self.cleaned = True

    async def send(self, data: bytes) -> None:
        return None

    async def wait_for_rx(self) -> bytes:
        await asyncio.Event().wait()
        return b""

    def set_rx_callback(self, callback: object) -> None:
        return None

    def configure_radio(self, **kwargs: object) -> bool:
        self.kwargs.update(kwargs)
        return True

    def set_tx_power(self, power: int) -> bool:
        self.kwargs["tx_power"] = power
        return True

    def sleep(self) -> None:
        return None

    def get_last_rssi(self) -> int:
        return -90

    def get_last_snr(self) -> float:
        return 5.0


def _fake_init(  # noqa: ANN202
    self,  # noqa: ANN001
    bus_id=1,  # noqa: ANN001
    reset_pin=25,  # noqa: ANN001
    frequency=0,  # noqa: ANN001
    tx_power=0,  # noqa: ANN001
    preamble_length=12,  # noqa: ANN001 - the library's own default, which must never survive
    **kwargs,  # noqa: ANN003
):
    _FakeRadio.__init__(
        self,
        bus_id=bus_id,
        reset_pin=reset_pin,
        frequency=frequency,
        tx_power=tx_power,
        preamble_length=preamble_length,
        **kwargs,
    )


class _Node:
    """One run of the node in this process, stopped on request instead of by stdin."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
        import openhop_core.hardware.sx1262_wrapper as sx1262

        fake = type("SX1262Radio", (_FakeRadio,), {"__init__": _fake_init})
        monkeypatch.setattr(sx1262, "SX1262Radio", fake)
        monkeypatch.setattr(radionode, "_check_device", lambda *a: None)
        self.stop = asyncio.Event()

        async def until_stopped() -> None:
            await self.stop.wait()

        monkeypatch.setattr(radionode, "_until_told_to_stop", until_stopped)
        self.state = state
        self.status: dict = {}
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> int:
        ready = asyncio.Event()

        def report(message: dict) -> None:
            self.status = message
            ready.set()

        config = {"state_dir": str(self.state), "wiring": {"bus_id": 1}, "seed": {}}
        self.task = asyncio.create_task(radionode.run_node(config, report))
        waiter = asyncio.create_task(ready.wait())
        await asyncio.wait({self.task, waiter}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
        if self.task.done():
            self.task.result()  # surface the node's own failure
        assert self.status.get("event") == "ready", self.status
        return int(self.status["port"])

    async def __aexit__(self, *exc: object) -> None:
        self.stop.set()
        assert self.task is not None
        assert await asyncio.wait_for(self.task, timeout=20) == 0


async def _client(port: int):  # noqa: ANN202
    from meshterm.core.connection import MeshCoreDevice

    device = MeshCoreDevice(transport="tcp", host="127.0.0.1", tcp_port=port)
    await device.connect()
    return device


@pytest.mark.asyncio
async def test_channels_name_and_radio_survive_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this node was built to end: a channel added through MeshTerm is forgotten."""
    secret = bytes(range(16))
    async with _Node(monkeypatch, tmp_path) as port:
        device = await _client(port)
        try:
            await device.set_channel(2, "Lakeside ops", secret)
            await device.set_name("Lakeside")
            await device.set_radio(869.525, 250.0, 11, 5)
        finally:
            await device.disconnect()
    assert _FakeRadio.instances[-1].cleaned, "the radio must be released on the way out"

    async with _Node(monkeypatch, tmp_path) as port:
        device = await _client(port)
        try:
            channel = await device.get_channel(2)
            info = await device.get_self_info()
        finally:
            await device.disconnect()
    assert channel is not None and channel.get("channel_name") == "Lakeside ops"
    assert bytes(channel.get("channel_secret") or b"")[:16] == secret
    assert info.get("name") == "Lakeside"
    # The chip is brought up on the saved radio, not the seed — and at SF11 with the
    # preamble MeshCore sends there.
    assert _FakeRadio.instances[-1].kwargs["frequency"] == 869_525_000
    assert _FakeRadio.instances[-1].preamble_length == 16


@pytest.mark.asyncio
async def test_a_cleared_channel_stays_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing a slot through MeshTerm removes it from what the node restores."""
    async with _Node(monkeypatch, tmp_path) as port:
        device = await _client(port)
        try:
            await device.set_channel(1, "#gone", bytes(16))
            await device.set_channel(1, "", None)
        finally:
            await device.disconnect()
    saved = json.loads((tmp_path / "channels.json").read_text(encoding="utf-8"))
    assert [c["idx"] for c in saved] == []


@pytest.mark.asyncio
async def test_the_identity_is_the_same_node_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node comes back with the key it had, so it is still the same contact to everyone."""
    keys = []
    for _ in range(2):
        async with _Node(monkeypatch, tmp_path) as port:
            device = await _client(port)
            try:
                keys.append((await device.get_self_info()).get("public_key"))
            finally:
                await device.disconnect()
    assert keys[0] and keys[0] == keys[1]
