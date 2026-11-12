"""Flood waits: reads wait out a short one once, checks stay inside their budget, writes
return at once. Time is a fake clock that the broker's and the fake's sleeps step."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from support.brokers import FakeClock, broker_starter, save_fake_session
from support.rpc import RawClient
from support.writes import check, confirm, hook, list_chats, send
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import FloodWait
from telegram_mcp.paths import FixedPaths


@pytest.fixture
def paths(home: Path) -> FixedPaths:
    return FixedPaths.from_home(home)


@pytest.fixture
def clock(fake_gateway: FakeGateway) -> FakeClock:
    clock = FakeClock()
    fake_gateway.sleep = clock.sleep
    return clock


@pytest.fixture
async def broker(
    paths: FixedPaths, fake_gateway: FakeGateway, clock: FakeClock
) -> AsyncIterator[Broker]:
    save_fake_session()
    start: Callable[..., Awaitable[Broker]]
    async with broker_starter(paths, fake_gateway, clock) as start:
        yield await start(sleep=clock.sleep)


async def test_a_read_waits_out_a_10_second_flood_wait_and_runs_once_more(
    broker: Broker, paths: FixedPaths, fake_gateway: FakeGateway, clock: FakeClock
) -> None:
    server = await RawClient.connect(paths)
    fake_gateway.script("list_chats", error=FloodWait(10))
    before = clock.now
    assert (await list_chats(server))["ok"] is True
    assert (clock.now - before).total_seconds() == 10
    assert fake_gateway.call_names().count("list_chats") == 2


async def test_a_read_with_a_flood_wait_over_30_seconds_answers_at_once(
    broker: Broker, paths: FixedPaths, fake_gateway: FakeGateway, clock: FakeClock
) -> None:
    server = await RawClient.connect(paths)
    fake_gateway.script("list_chats", error=FloodWait(120))
    before = clock.now
    failed = await list_chats(server)
    assert failed["error"]["code"] == "flood_wait"
    assert failed["error"]["retry_after"] == 120
    assert "120 s" in failed["error"]["message"]
    assert clock.now == before
    assert fake_gateway.call_names().count("list_chats") == 1


async def test_two_flood_waits_in_a_row_answer_after_one_wait(
    broker: Broker, paths: FixedPaths, fake_gateway: FakeGateway, clock: FakeClock
) -> None:
    server = await RawClient.connect(paths)
    fake_gateway.script("list_chats", error=FloodWait(10), times=2)
    before = clock.now
    failed = await list_chats(server)
    assert (failed["error"]["code"], failed["error"]["retry_after"]) == ("flood_wait", 10)
    assert (clock.now - before).total_seconds() == 10
    assert fake_gateway.call_names().count("list_chats") == 2


@pytest.mark.parametrize(("first_call", "waited"), [(10, True), (15, False)])
async def test_a_read_waits_only_when_its_second_try_still_answers_inside_70_seconds(
    broker: Broker,
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    clock: FakeClock,
    first_call: float,
    waited: bool,
) -> None:
    # Arrival + first call + 30 s wait + 30 s for the second call must stay within 70 s.
    server = await RawClient.connect(paths)
    fake_gateway.script("list_chats", delay=first_call, error=FloodWait(30))
    result = await list_chats(server)
    assert result["ok"] is waited
    assert fake_gateway.call_names().count("list_chats") == (2 if waited else 1)


@pytest.mark.parametrize(("wait", "asked"), [(3, True), (8, False)])
async def test_a_check_waits_out_a_flood_wait_only_inside_its_10_second_budget(
    broker: Broker,
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    wait: int,
    asked: bool,
) -> None:
    # The first lookup takes 4 s, so 6 s of the budget are left.
    fake_gateway.script("resolve_chat", delay=4, error=FloodWait(wait))
    answer = await check(await hook(paths), "after a flood wait")
    if asked:
        assert answer["decision"] == "ask"
        assert fake_gateway.call_names().count("resolve_chat") == 2
    else:
        assert (answer["decision"], answer["code"]) == ("refuse", "flood_wait")
        assert fake_gateway.call_names().count("resolve_chat") == 1
        assert broker.gate.open_count() == 0


async def test_a_write_answers_a_flood_wait_at_once_and_logs_it_with_its_approval(
    broker: Broker, paths: FixedPaths, fake_gateway: FakeGateway, clock: FakeClock
) -> None:
    approval_id = await confirm(paths, "throttled")
    fake_gateway.script("send_text", error=FloodWait(25))
    server = await RawClient.connect(paths)
    before, started = clock.now, time.monotonic()
    refused = await send(server, "throttled")
    assert time.monotonic() - started < 1
    assert clock.now == before
    assert refused["error"] == {
        "code": "flood_wait",
        "message": "Telegram asked to wait 25 s; nothing was sent. Ask again after that.",
        "retry_after": 25,
    }
    assert fake_gateway.call_names().count("send_text") == 1
    results = [
        json.loads(line)
        for line in paths.log.read_text().splitlines()
        if '"event":"result"' in line and approval_id in line
    ]
    assert [entry["result"] for entry in results] == ["flood_wait"]
