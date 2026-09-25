# SPDX-License-Identifier: Apache-2.0
"""Tests for the clock set: the shared device step, and the on-connect service around it.

One function sets the device clock for both the Device config action and the automatic
service; the service is off by default, acts once per connection in the background, and
says what it did in the log rather than on screen.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.connection import ClockAheadError
from meshterm.core.device_store import DeviceStore
from meshterm.core.preferences import get_spec
from meshterm.persistence.repository import Repository
from meshterm.services.clock_sync import ClockSync, set_clock


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """A mock-backed application context, preferences at their defaults."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "clock.db")
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


@pytest.fixture()
def log_lines() -> list[str]:
    """Every record the app logger emits during the test (it does not propagate to root)."""
    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = _Collect(level=logging.DEBUG)
    logger = logging.getLogger("meshterm")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield lines
    logger.removeHandler(handler)
    logger.setLevel(previous)


async def _settle() -> None:
    """Let a task spawned by the service run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


def test_the_preference_exists_and_is_off_by_default() -> None:
    """Writing to a radio nobody asked to be written to is the owner's call."""
    spec = get_spec("set_clock_on_connect")
    assert spec.value_type == "bool"
    assert spec.default is False
    assert spec.group == "Device"


async def test_set_clock_writes_the_host_time_and_reports_the_drift(ctx: AppContext) -> None:
    """The shared step corrects a clock an hour slow and says by how much."""
    device = await ctx.device()
    device._clock_offset = -3600  # noqa: SLF001 - an hour slow; a write could only go forward

    done = await set_clock(device)

    assert abs(done.epoch - int(time.time())) <= 2
    assert done.drift_s is not None and -3602 <= done.drift_s <= -3598
    assert abs(await device.get_time() - int(time.time())) <= 2


async def test_the_service_does_nothing_while_the_preference_is_off(
    ctx: AppContext, log_lines: list[str]
) -> None:
    """Off by default: a connection is noted and the clock is left alone."""
    device = await ctx.device()
    device._clock_offset = -3600  # noqa: SLF001 - an hour slow; a write could only go forward
    service = ClockSync(ctx)
    await service.start()

    service.on_connected(device)
    await _settle()

    assert abs(await device.get_time() - (int(time.time()) - 3600)) <= 2
    assert not any("clock sync" in line for line in log_lines)


async def test_the_service_sets_the_clock_once_per_connection_and_logs_it(
    ctx: AppContext, log_lines: list[str]
) -> None:
    """On: the first settled connection is set in the background; the same one is not set twice."""
    ctx.preferences.set("set_clock_on_connect", True)
    device = await ctx.device()
    device._clock_offset = -3600  # noqa: SLF001 - an hour slow; a write could only go forward
    service = ClockSync(ctx)
    await service.start()  # a device is already connected: that counts as the first one

    await _settle()
    assert abs(await device.get_time() - int(time.time())) <= 2
    said = [line for line in log_lines if line.startswith("clock sync: device clock set to")]
    assert len(said) == 1
    assert "-36" in said[0]  # "(was -3600 s off)", give or take the seconds the test took

    device._clock_offset = -3600  # noqa: SLF001 - an hour slow; a write could only go forward
    service.on_connected(device)
    await _settle()
    assert abs(await device.get_time() - (int(time.time()) - 3600)) <= 2  # untouched
    assert len([line for line in log_lines if "device clock set to" in line]) == 1
    await service.aclose()


async def test_a_service_never_started_ignores_connections(
    ctx: AppContext, log_lines: list[str]
) -> None:
    """A scripted run never starts the service, so its connections never write the clock."""
    ctx.preferences.set("set_clock_on_connect", True)
    device = await ctx.device()
    device._clock_offset = -3600  # noqa: SLF001 - an hour slow; a write could only go forward

    ctx.clock_sync.on_connected(device)  # what the connect path does
    await _settle()

    assert abs(await device.get_time() - (int(time.time()) - 3600)) <= 2
    assert not any("clock sync" in line for line in log_lines)


async def test_a_write_that_fails_is_logged_not_raised(
    ctx: AppContext, log_lines: list[str]
) -> None:
    """A firmware that refuses the write costs a warning line, never the session."""
    ctx.preferences.set("set_clock_on_connect", True)
    device = await ctx.device()

    async def _refuse(epoch: int) -> None:
        raise RuntimeError("no clock here")

    device.set_time = _refuse  # type: ignore[method-assign]
    service = ClockSync(ctx)
    await service.start()
    await _settle()

    assert any("could not set the device clock: no clock here" in line for line in log_lines)


async def test_a_clock_a_few_seconds_ahead_is_in_sync_and_left_alone(ctx: AppContext) -> None:
    """Firmware never sets its clock back; a few seconds ahead needs no setting anyway."""
    device = await ctx.device()
    device._clock_offset = 5  # noqa: SLF001 - e.g. a GPS-disciplined radio beside a slow PC

    done = await set_clock(device)

    assert not done.written
    assert done.drift_s is not None and 3 <= done.drift_s <= 6


async def test_a_clock_far_ahead_is_stated_not_retried(ctx: AppContext) -> None:
    """Minutes ahead: nothing is written, and the error says why and what resets it."""
    device = await ctx.device()
    device._clock_offset = 1800  # noqa: SLF001

    with pytest.raises(ClockAheadError) as caught:
        await set_clock(device)
    assert caught.value.ahead_s is not None and 1795 <= caught.value.ahead_s <= 1801
    assert "30 min ahead" in str(caught.value) and "rebooting the radio" in str(caught.value)


async def test_the_simulator_refuses_to_set_its_clock_back_like_the_firmware() -> None:
    """``CMD_SET_DEVICE_TIME`` with an earlier time is refused, never applied."""
    from meshterm.core.connection import MockDevice

    device = MockDevice()
    await device.connect()
    now = int(time.time())
    await device.set_time(now + 100)
    with pytest.raises(ClockAheadError):
        await device.set_time(now)
    assert await device.get_time() >= now + 99


async def test_the_firmwares_refusal_reads_as_a_clock_ahead_not_malformed() -> None:
    """The real device turns ERR_CODE_ILLEGAL_ARG on a set-time into ClockAheadError."""
    from types import SimpleNamespace

    from meshterm.core.connection import MeshCoreDevice

    class _Commands:
        async def set_time(self, epoch):  # noqa: ANN001, ANN201
            return SimpleNamespace(is_error=lambda: True, payload={"error_code": 6})

    device = object.__new__(MeshCoreDevice)
    device._mc = SimpleNamespace(commands=_Commands())  # noqa: SLF001
    with pytest.raises(ClockAheadError) as caught:
        await device.set_time(int(time.time()))
    assert "malformed" not in str(caught.value)


async def test_the_service_logs_a_clock_ahead_plainly(
    ctx: AppContext, log_lines: list[str]
) -> None:
    """On connect, a clock far ahead is one clear warning, not a 'malformed' request."""
    ctx.preferences.set("set_clock_on_connect", True)
    device = await ctx.device()
    device._clock_offset = 7200  # noqa: SLF001
    service = ClockSync(ctx)
    await service.start()
    service.on_connected(device)
    await _settle()
    assert any("left the device clock alone" in line and "2 h ahead" in line for line in log_lines)
    assert not any("malformed" in line for line in log_lines)
    await service.aclose()
