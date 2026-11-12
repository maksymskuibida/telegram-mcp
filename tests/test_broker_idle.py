"""Idle exit after ten minutes with no request, its stop order, and what status reports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from support.brokers import FakeClock, broker_starter, save_fake_session, wait_until
from support.rpc import RawClient
from support.writes import check, hook, list_chats
from telegram_mcp.broker.lock import LockProbe, probe_lock, read_lock
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.paths import FixedPaths


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def start_broker(
    paths: FixedPaths, fake_gateway: FakeGateway, clock: FakeClock
) -> AsyncIterator[Callable[..., Awaitable[Broker]]]:
    save_fake_session()
    async with broker_starter(paths, fake_gateway, clock) as start:
        yield start


async def test_the_broker_stops_after_ten_idle_minutes_with_sessions_still_connected(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, clock: FakeClock
) -> None:
    broker = await start_broker()
    terminal, desktop = await RawClient.connect(paths), await RawClient.connect(paths)
    assert (await list_chats(terminal))["ok"] is True
    clock.advance(599)
    broker.sweep()
    await asyncio.sleep(0.05)
    assert paths.socket.exists()
    clock.advance(1)
    broker.sweep()
    await asyncio.wait_for(broker.wait_stopped(), 3)
    assert await asyncio.wait_for(desktop.reader.read(), 2) == b""
    assert not paths.socket.exists()
    assert not paths.secret.exists()
    assert (read_lock(paths.lock) or {}).get("state") == "released"
    assert "idle stop after 10 minutes with no request" in paths.diagnostics.read_text()


async def test_the_broker_does_not_stop_while_a_read_runs(
    start_broker: Callable[..., Awaitable[Broker]],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    broker = await start_broker()
    reader = await RawClient.connect(paths)
    fake_gateway.script("list_chats", delay=0.3)
    reading = asyncio.create_task(list_chats(reader))
    await wait_until(lambda: "list_chats" in fake_gateway.call_names())
    clock.advance(11 * 60)
    broker.sweep()
    assert broker.running_requests == 1
    assert (await reading)["ok"] is True
    broker.sweep()
    await asyncio.sleep(0.05)
    assert paths.socket.exists()  # answering counts as activity


async def test_the_stop_runs_in_order_and_holds_the_lock_until_telegram_is_left(
    start_broker: Callable[..., Awaitable[Broker]],
    paths: FixedPaths,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = await start_broker()
    connected = await RawClient.connect(paths)
    assert (await list_chats(connected))["ok"] is True
    seen: dict[str, Any] = {}
    leave_telegram = broker.link.close

    async def observe_then_disconnect(limit: float = 2.0) -> None:
        seen["socket"] = paths.socket.exists()
        seen["secret"] = paths.secret.exists()
        seen["client"] = await asyncio.wait_for(connected.reader.read(), 1)
        seen["lock"] = probe_lock(paths.lock)
        seen["limit"] = limit
        await leave_telegram(limit)
        seen["lock after"] = probe_lock(paths.lock)

    monkeypatch.setattr(broker.link, "close", observe_then_disconnect)
    clock.advance(600)
    broker.sweep()
    await asyncio.wait_for(broker.wait_stopped(), 3)
    assert seen == {
        "socket": False,
        "secret": False,
        "client": b"",
        "lock": LockProbe.RUNNING,
        "limit": 2.0,
        "lock after": LockProbe.RUNNING,
    }
    assert probe_lock(paths.lock) is LockProbe.FREE


async def test_a_request_arriving_during_the_stop_is_not_served(
    start_broker: Callable[..., Awaitable[Broker]],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = await start_broker()
    late = await RawClient.connect(paths)
    stopping = asyncio.Event()
    finish = asyncio.Event()
    leave_telegram = broker.link.close

    async def slow_disconnect(limit: float = 2.0) -> None:
        stopping.set()
        await finish.wait()
        await leave_telegram(limit)

    monkeypatch.setattr(broker.link, "close", slow_disconnect)
    clock.advance(600)
    broker.sweep()
    await asyncio.wait_for(stopping.wait(), 2)
    request = {"jsonrpc": "2.0", "id": 5, "method": "tool.call", "params": {"tool": "x"}}
    late.writer.write(json.dumps(request).encode() + b"\n")
    assert await asyncio.wait_for(late.reader.read(), 2) == b""
    with pytest.raises(FileNotFoundError):  # the socket was deleted first
        await RawClient.open(paths)
    finish.set()
    await asyncio.wait_for(broker.wait_stopped(), 3)
    assert "list_chats" not in fake_gateway.call_names()


async def test_an_idle_stop_never_drops_a_live_approval(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, clock: FakeClock
) -> None:
    broker = await start_broker()
    asked = await check(await hook(paths), "left unanswered")
    clock.advance(600)
    assert broker.gate.open_count() == 0  # expired after 120 s, long before the idle limit
    broker.sweep()
    await asyncio.wait_for(broker.wait_stopped(), 3)
    timed_out = [
        json.loads(line)
        for line in paths.log.read_text().splitlines()
        if asked["approval_id"] in line and '"timed_out"' in line
    ]
    assert len(timed_out) == 1


async def test_status_counts_the_two_sessions_not_the_hook_or_the_status_command(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths
) -> None:
    await start_broker()
    sessions = [await RawClient.connect(paths) for _ in range(2)]
    approval_hook = await hook(paths)
    status_client = await RawClient.connect(paths, client="cli")
    assert (await status_client.result("broker.status"))["clients"] == 2
    await sessions[0].close()
    await asyncio.sleep(0.05)
    assert (await status_client.result("broker.status"))["clients"] == 1
    await approval_hook.close()


async def test_idle_seconds_is_the_time_idle_before_the_status_request(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, clock: FakeClock
) -> None:
    await start_broker()
    status_client = await RawClient.connect(paths, client="cli")
    clock.advance(42)
    assert (await status_client.result("broker.status"))["idle_seconds"] == 42
    assert (await status_client.result("broker.status"))["idle_seconds"] == 0
    clock.advance(7.9)
    assert (await status_client.result("broker.status"))["idle_seconds"] == 7


async def test_the_sweep_runs_the_timers_on_its_own(
    start_broker: Callable[..., Awaitable[Broker]], paths: FixedPaths, clock: FakeClock
) -> None:
    broker = await start_broker(sweep_interval=0.02)
    clock.advance(601)
    await asyncio.wait_for(broker.wait_stopped(), 3)
    assert not paths.socket.exists()
