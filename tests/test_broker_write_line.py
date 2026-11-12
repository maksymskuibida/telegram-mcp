"""When a write's approval is used, and that each approval sends at most once.

Written before the write line exists (permission logic, so tests first). A write's approval is
used when the write enters the write line: every refusal before that keeps it, everything after
spends it. Nothing in the broker ever sends a write twice."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from support.brokers import FakeClock, save_fake_session, wait_until
from support.rpc import RawClient
from support.writes import check, confirm, hook, list_chats, send
from telegram_mcp.broker.scheduler import (
    DropReason,
    LineFull,
    WriteDropped,
    WriteItem,
    WriteLine,
)
from telegram_mcp.broker.server import Broker
from telegram_mcp.gateway.fake import FakeGateway
from telegram_mcp.gateway.models import FloodWait, GatewayTimeout, OutcomeUnknown
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
) -> AsyncIterator[Callable[..., Any]]:
    save_fake_session()
    started: list[Broker] = []

    async def start(**options: Any) -> Broker:
        broker = Broker(
            paths,
            gateway_factory=lambda session, api_id, api_hash: fake_gateway,
            clock=clock,
            **options,
        )
        await broker.start()
        started.append(broker)
        return broker

    yield start
    for broker in started:
        await broker.stop()


def send_attempts(fake: FakeGateway, text: str | None = None) -> int:
    return sum(
        1
        for name, args in fake.calls
        if name == "send_text" and (text is None or args["text"] == text)
    )


async def drop_the_telegram_link(paths: FixedPaths, fake: FakeGateway) -> None:
    """A read that times out marks the one Telegram connection as dropped."""
    fake.script("list_chats", error=GatewayTimeout())
    reader = await RawClient.connect(paths)
    assert (await list_chats(reader))["error"]["code"] == "telegram_timeout"
    await reader.close()


# Refusals before the line keep the approval ------------------------------------------------


async def test_a_write_refused_for_no_session_keeps_its_approval(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    await confirm(paths, "kept 1")
    server = await RawClient.connect(paths)
    fake_gateway.authorized = False
    assert (await send(server, "kept 1"))["error"]["code"] == "no_session"
    fake_gateway.authorized = True
    assert (await send(server, "kept 1"))["ok"] is True
    assert send_attempts(fake_gateway) == 1


async def test_a_write_refused_while_telegram_cannot_be_reached_keeps_its_approval(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    await start_broker()
    await confirm(paths, "kept 2")
    await drop_the_telegram_link(paths, fake_gateway)
    fake_gateway.script("connect", error=GatewayTimeout())
    server = await RawClient.connect(paths)
    refused = await send(server, "kept 2")
    assert refused["error"]["code"] == "telegram_error"
    assert refused["error"]["retry_after"] == 1
    assert send_attempts(fake_gateway) == 0
    clock.advance(1)
    assert (await send(server, "kept 2"))["ok"] is True
    assert send_attempts(fake_gateway) == 1


async def test_a_write_refused_because_the_line_is_full_keeps_its_approval(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    broker = await start_broker()
    texts = [f"line {number}" for number in range(22)]
    for text in texts:
        await confirm(paths, text)
    fake_gateway.script("send_text", hang=True)
    clients = [await RawClient.connect(paths) for _ in range(22)]
    waiting = [asyncio.create_task(send(clients[0], texts[0], 30))]
    await wait_until(lambda: send_attempts(fake_gateway) == 1)
    for number in range(1, 21):
        waiting.append(asyncio.create_task(send(clients[number], texts[number], 30)))
        await wait_until(lambda n=number: broker.writes.waiting == n)  # type: ignore[misc]
    refused = await send(clients[21], texts[21])
    assert refused["error"]["code"] == "telegram_error"
    assert "too many sends waiting" in refused["error"]["message"]
    assert await check(await hook(paths), texts[21]) == {"decision": "allow"}
    assert send_attempts(fake_gateway) == 1
    for task in waiting:
        task.cancel()


# Entering the line spends the approval -----------------------------------------------------


async def test_a_write_dropped_after_40_seconds_in_the_line_has_used_its_approval(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    broker = await start_broker()
    await confirm(paths, "ahead")
    behind_id = await confirm(paths, "behind")
    fake_gateway.script("send_text", hang=True)
    first, second = await RawClient.connect(paths), await RawClient.connect(paths)
    running = asyncio.create_task(send(first, "ahead", 30))
    await wait_until(lambda: send_attempts(fake_gateway) == 1)
    queued = asyncio.create_task(send(second, "behind"))
    await wait_until(lambda: broker.writes.waiting == 1)
    clock.advance(41)
    broker.sweep()
    dropped = await queued
    assert dropped["error"]["code"] == "telegram_timeout"
    assert "not sent" in dropped["error"]["message"].lower()
    assert (await send(second, "behind"))["error"]["code"] == "needs_confirmation"
    assert send_attempts(fake_gateway, "behind") == 0
    diagnostics = paths.diagnostics.read_text()
    assert (
        f"send not started (not started in time): tg_messages_send, approval {behind_id}"
        in diagnostics
    )
    assert "behind" not in diagnostics.replace("approval", "")  # never the message text
    running.cancel()


async def test_a_write_refused_by_a_flood_wait_has_used_its_approval(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    await confirm(paths, "flooded")
    fake_gateway.script("send_text", error=FloodWait(5))
    server = await RawClient.connect(paths)
    started = time.monotonic()
    refused = await send(server, "flooded")
    assert time.monotonic() - started < 1
    assert refused["error"]["code"] == "flood_wait"
    assert refused["error"]["retry_after"] == 5
    assert "nothing was sent" in refused["error"]["message"]
    assert (await send(server, "flooded"))["error"]["code"] == "needs_confirmation"
    assert send_attempts(fake_gateway) == 1


async def test_a_write_that_timed_out_has_used_its_approval(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker(call_limit=0.3)
    await confirm(paths, "slow")
    fake_gateway.script("send_text", hang=True)
    server = await RawClient.connect(paths)
    started = time.monotonic()
    assert (await send(server, "slow"))["error"]["code"] == "outcome_unknown"
    assert time.monotonic() - started < 2
    assert (await send(server, "slow"))["error"]["code"] == "needs_confirmation"
    assert send_attempts(fake_gateway) == 1


async def test_a_sent_write_has_used_its_approval(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    await confirm(paths, "once")
    server = await RawClient.connect(paths)
    assert (await send(server, "once"))["ok"] is True
    assert (await send(server, "once"))["error"]["code"] == "needs_confirmation"
    assert [sent.text for sent in fake_gateway.sent] == ["once"]


# Never twice --------------------------------------------------------------------------------


async def test_a_write_lost_with_a_dropped_connection_is_never_sent_again(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    await start_broker()
    await confirm(paths, "dropped")
    fake_gateway.script("send_text", drop_after_send=True)
    server = await RawClient.connect(paths)
    assert (await send(server, "dropped"))["error"]["code"] == "outcome_unknown"
    assert (await list_chats(server))["ok"] is True
    assert (await send(server, "dropped"))["error"]["code"] == "needs_confirmation"
    assert send_attempts(fake_gateway) == 1


async def test_a_write_running_at_the_idle_limit_is_sent_once_and_the_broker_waits(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    broker = await start_broker()
    await confirm(paths, "at the limit")
    fake_gateway.script("send_text", delay=0.3)
    server = await RawClient.connect(paths)
    running = asyncio.create_task(send(server, "at the limit"))
    await wait_until(lambda: send_attempts(fake_gateway) == 1)
    clock.advance(11 * 60)
    broker.sweep()
    await asyncio.sleep(0.05)
    assert paths.socket.exists()
    assert (await running)["ok"] is True
    clock.advance(11 * 60)
    broker.sweep()
    await asyncio.wait_for(broker.wait_stopped(), 3)
    assert send_attempts(fake_gateway) == 1


async def test_a_queued_write_whose_caller_left_is_not_started(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    broker = await start_broker()
    await confirm(paths, "first")
    approval_id = await confirm(paths, "left behind")
    fake_gateway.script("send_text", delay=0.4)
    first, leaving = await RawClient.connect(paths), await RawClient.connect(paths)
    running = asyncio.create_task(send(first, "first"))
    await wait_until(lambda: send_attempts(fake_gateway) == 1)
    request = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tool.call",
        "params": {"tool": "tg_messages_send", "args": {"chat": 101, "text": "left behind"}},
    }
    leaving.writer.write(json.dumps(request).encode() + b"\n")
    await leaving.writer.drain()
    await wait_until(lambda: broker.writes.waiting == 1)
    await leaving.close()
    assert (await running)["ok"] is True
    await wait_until(lambda: broker.writes.waiting == 0)
    await asyncio.sleep(0.05)
    assert send_attempts(fake_gateway, "left behind") == 0
    again = await RawClient.connect(paths)
    assert (await send(again, "left behind"))["error"]["code"] == "needs_confirmation"
    results = [
        json.loads(line)
        for line in paths.log.read_text().splitlines()
        if approval_id in line and '"event":"result"' in line
    ]
    assert [entry["result"] for entry in results] == ["telegram_timeout"]


async def test_the_40_seconds_count_from_arrival_and_include_the_connect(
    start_broker: Callable[..., Any],
    paths: FixedPaths,
    fake_gateway: FakeGateway,
    clock: FakeClock,
) -> None:
    broker = await start_broker()
    fake_gateway.sleep = clock.sleep
    await confirm(paths, "running")
    await confirm(paths, "late")
    fake_gateway.script("send_text", hang=True)
    first, second = await RawClient.connect(paths), await RawClient.connect(paths)
    running = asyncio.create_task(send(first, "running", 30))
    await wait_until(lambda: send_attempts(fake_gateway) == 1)
    await drop_the_telegram_link(paths, fake_gateway)
    fake_gateway.script("connect", delay=25)
    late = asyncio.create_task(send(second, "late"))
    await wait_until(lambda: broker.writes.waiting == 1)
    clock.advance(20)
    broker.sweep()
    assert (await late)["error"]["code"] == "telegram_timeout"
    assert send_attempts(fake_gateway, "late") == 0
    running.cancel()


# The line on its own -------------------------------------------------------------------------


def line_item(clock: FakeClock, run: Callable[[], Any], is_open: bool = True) -> WriteItem:
    return WriteItem(
        tool="tg_messages_send",
        approval_id="a" * 10,
        arrived_at=clock(),
        is_open=lambda: is_open,
        run=run,
    )


async def test_the_line_refuses_past_its_capacity_and_drops_on_its_own_timer(
    clock: FakeClock,
) -> None:
    dropped: list[DropReason] = []
    line = WriteLine(
        clock=clock, capacity=1, start_limit=0.2, on_drop=lambda item, why: dropped.append(why)
    )
    line.start()
    release = asyncio.Event()

    async def blocked() -> str:
        await release.wait()
        return "sent"

    running = asyncio.create_task(line.submit(line_item(clock, blocked)))
    await wait_until(lambda: line.running is not None)
    waiting = asyncio.create_task(line.submit(line_item(clock, blocked)))
    await wait_until(lambda: line.waiting == 1)
    assert line.is_full
    with pytest.raises(LineFull):
        await line.submit(line_item(clock, blocked))
    with pytest.raises(WriteDropped) as late:
        await waiting
    assert late.value.reason is DropReason.DEADLINE
    assert dropped == [DropReason.DEADLINE]
    release.set()
    assert await running == "sent"
    await line.close()


async def test_closing_the_line_drops_waiting_writes_and_ends_the_running_one_unknown(
    clock: FakeClock,
) -> None:
    line = WriteLine(clock=clock)
    line.start()

    async def forever() -> None:
        await asyncio.Event().wait()

    async def failing() -> None:
        raise RuntimeError("refused")

    running = asyncio.create_task(line.submit(line_item(clock, forever)))
    await wait_until(lambda: line.running is not None)
    waiting = asyncio.create_task(line.submit(line_item(clock, forever)))
    await wait_until(lambda: line.waiting == 1)
    await line.close()
    with pytest.raises(WriteDropped) as stopped:
        await waiting
    assert stopped.value.reason is DropReason.STOPPING
    with pytest.raises(OutcomeUnknown):
        await running
    line.start()
    with pytest.raises(RuntimeError, match="refused"):
        await line.submit(line_item(clock, failing))
    with pytest.raises(WriteDropped) as left:
        await line.submit(line_item(clock, failing, is_open=False))
    assert left.value.reason is DropReason.CALLER_LEFT
    assert not line.busy
    await line.close()


async def test_writes_run_one_at_a_time_in_arrival_order(
    start_broker: Callable[..., Any], paths: FixedPaths, fake_gateway: FakeGateway
) -> None:
    broker = await start_broker()
    texts = ["one", "two", "three", "four"]
    for text in texts:
        await confirm(paths, text)
    fake_gateway.script("send_text", delay=0.3)
    clients = [await RawClient.connect(paths) for _ in texts]
    tasks = [asyncio.create_task(send(clients[0], texts[0]))]
    await wait_until(lambda: send_attempts(fake_gateway) == 1)
    for number in range(1, 4):
        tasks.append(asyncio.create_task(send(clients[number], texts[number])))
        await wait_until(lambda n=number: broker.writes.waiting == n)  # type: ignore[misc]
    assert send_attempts(fake_gateway) == 1
    results = await asyncio.gather(*tasks)
    assert all(result["ok"] for result in results)
    assert [sent.text for sent in fake_gateway.sent] == texts


async def test_waiting_for_a_quiet_line_returns_once_the_running_and_waiting_writes_are_done(
    clock: FakeClock,
) -> None:
    line = WriteLine(clock=clock)
    await asyncio.wait_for(line.wait_idle(), 1)  # an empty line is quiet at once
    line.start()
    release = asyncio.Event()
    order: list[str] = []

    async def blocked(name: str) -> str:
        await release.wait()
        order.append(name)
        return name

    first = asyncio.create_task(line.submit(line_item(clock, lambda: blocked("first"))))
    await wait_until(lambda: line.running is not None)
    second = asyncio.create_task(line.submit(line_item(clock, lambda: blocked("second"))))
    await wait_until(lambda: line.waiting == 1)
    quiet = asyncio.create_task(line.wait_idle())
    await asyncio.sleep(0.05)
    assert not quiet.done()
    release.set()
    await asyncio.wait_for(quiet, 1)
    assert order == ["first", "second"]
    assert (await first, await second) == ("first", "second")
    assert not line.busy
    await line.close()
